"""Analisador de Avaliações com IA — painel para donos de restaurante.

Sobe um CSV de avaliações, a IA (Google Gemini) classifica cada uma por
sentimento, tema e urgência, e o painel mostra os resultados e sugere
respostas para as avaliações negativas.
"""

import json
import os
import time

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv()

# Modelo principal e modelo reserva (usado se o principal estiver sobrecarregado)
MODELOS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
TAMANHO_LOTE = 20  # avaliações por chamada à API (menos chamadas = menos limite estourado)

TEMAS = [
    "entrega",
    "qualidade da comida",
    "temperatura",
    "embalagem",
    "atendimento",
    "preço/porção",
    "pedido errado",
    "higiene/saúde",
    "pagamento",
    "outro",
]
COLUNAS_OBRIGATORIAS = ["id", "data", "cliente", "nota", "comentario"]

PROMPT_CLASSIFICACAO = """Você analisa avaliações de clientes de um restaurante de delivery.
Para CADA avaliação abaixo, devolva um objeto com:
- "id": o mesmo id da avaliação
- "sentimento": "positivo", "neutro" ou "negativo"
- "tema": o tema principal, exatamente um destes: {temas}
- "urgente": true se envolver saúde, higiene, cobrança indevida ou cancelamento; senão false
- "resumo": frase curta em português, com no máximo 10 palavras

Responda SOMENTE com uma lista JSON, sem texto extra.

Avaliações:
{avaliacoes}
"""

PROMPT_RESPOSTA = """Você é o gerente de um restaurante de delivery respondendo a uma avaliação negativa.
Escreva uma resposta curta (até 4 frases), educada e empática, em português do Brasil:
- agradeça o retorno e peça desculpas pelo problema específico;
- diga o que será feito para não acontecer de novo, sem prometer reembolsos ou descontos;
- convide o cliente a falar com o restaurante para resolver.
Não invente informações sobre o restaurante.

Cliente: {cliente}
Nota: {nota}/5
Avaliação: "{comentario}"
"""


# ---------------------------------------------------------------- API Gemini

@st.cache_resource
def cliente_gemini():
    chave = os.getenv("GEMINI_API_KEY")
    if not chave:
        return None
    return genai.Client(api_key=chave)


def chamar_gemini(prompt: str, json_saida: bool = False) -> str:
    """Chama o Gemini com novas tentativas e modelo reserva.

    Erros 429 (limite) e 5xx (sobrecarga) são temporários: espera e tenta de
    novo; se o modelo principal continuar falhando, passa para o reserva.
    """
    client = cliente_gemini()
    config = types.GenerateContentConfig(
        temperature=0.2,
        response_mime_type="application/json" if json_saida else "text/plain",
    )
    ultimo_erro = None
    for modelo in MODELOS:
        for tentativa in range(3):
            try:
                resposta = client.models.generate_content(
                    model=modelo, contents=prompt, config=config
                )
                return resposta.text
            except errors.APIError as e:
                ultimo_erro = e
                if e.code == 404:  # modelo indisponível: passa direto para o próximo
                    break
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** tentativa * 2)  # 2s, 4s, 8s
                    continue
                raise
    raise RuntimeError(
        "A API do Gemini está sobrecarregada no momento. "
        "Espere um minuto e tente de novo."
    ) from ultimo_erro


@st.cache_data(show_spinner=False)
def classificar_lote(texto_lote: str) -> list:
    """Classifica um lote de avaliações. O cache evita chamar a API de novo para o mesmo lote."""
    prompt = PROMPT_CLASSIFICACAO.format(temas=", ".join(TEMAS), avaliacoes=texto_lote)
    return json.loads(chamar_gemini(prompt, json_saida=True))


def classificar(df: pd.DataFrame) -> pd.DataFrame:
    """Classifica todas as avaliações em lotes (menos chamadas à API)."""
    resultados = []
    barra = st.progress(0.0, text="Classificando avaliações com IA...")
    for inicio in range(0, len(df), TAMANHO_LOTE):
        lote = df.iloc[inicio : inicio + TAMANHO_LOTE]
        texto = "\n".join(
            f'- id {r.id} (nota {r.nota}): "{r.comentario}"' for r in lote.itertuples()
        )
        resultados.extend(classificar_lote(texto))
        barra.progress(min(1.0, (inicio + len(lote)) / len(df)))
    barra.empty()

    classif = pd.DataFrame(resultados)
    classif["id"] = classif["id"].astype(df["id"].dtype)
    classif["tema"] = classif["tema"].where(classif["tema"].isin(TEMAS), "outro")
    classif["urgente"] = classif["urgente"].astype(bool)
    return df.merge(classif, on="id", how="left")


@st.cache_data(show_spinner=False)
def sugerir_resposta(cliente: str, nota: int, comentario: str) -> str:
    return chamar_gemini(
        PROMPT_RESPOSTA.format(cliente=cliente, nota=nota, comentario=comentario)
    ).strip()


# ---------------------------------------------------------------- Interface

st.set_page_config(page_title="Analisador de Avaliações com IA", page_icon="⭐", layout="wide")
st.title("⭐ Analisador de Avaliações com IA")
st.caption(
    "Suba as avaliações do seu restaurante e a IA mostra o que os clientes "
    "estão elogiando, do que reclamam e o que precisa de atenção agora."
)

if cliente_gemini() is None:
    st.error("Chave da API não encontrada. Crie um arquivo `.env` com `GEMINI_API_KEY=sua_chave`.")
    st.stop()

arquivo = st.file_uploader(
    "Arquivo CSV com as colunas: id, data, cliente, nota, comentario", type="csv"
)
if arquivo is None:
    st.info("👆 Suba um CSV para começar. Use o `avaliacoes.csv` de exemplo deste projeto.")
    st.stop()

df = pd.read_csv(arquivo)
faltando = [c for c in COLUNAS_OBRIGATORIAS if c not in df.columns]
if faltando:
    st.error(f"O CSV não tem as colunas: {', '.join(faltando)}")
    st.stop()

try:
    dados = classificar(df)
except Exception as e:  # mostra erro amigável em vez de travar o app
    st.error(f"Não foi possível classificar as avaliações: {e}")
    st.stop()

# --- Cartões
negativas = dados[dados["sentimento"] == "negativo"]
urgentes = dados[dados["urgente"]]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Avaliações", len(dados))
c2.metric("Nota média", f"{dados['nota'].mean():.1f} ★")
c3.metric("Negativas", f"{len(negativas) / len(dados):.0%}")
c4.metric("Urgentes", len(urgentes))

# --- Urgentes em destaque
if len(urgentes):
    st.subheader("⚠️ Urgentes: resolver hoje")
    for r in urgentes.itertuples():
        st.error(f"**{r.cliente}** · nota {r.nota} · *{r.tema}* — {r.comentario}")

# --- Gráficos
g1, g2 = st.columns(2)
with g1:
    st.subheader("Sentimento")
    ordem = ["positivo", "neutro", "negativo"]
    st.bar_chart(dados["sentimento"].value_counts().reindex(ordem, fill_value=0))
with g2:
    st.subheader("Temas mais citados nas reclamações")
    temas_neg = (
        dados[dados["sentimento"] != "positivo"]["tema"].value_counts().rename("count").rename_axis("tema")
    )
    st.bar_chart(temas_neg, horizontal=True, sort="-count")

# --- Tabela com filtros
st.subheader("Todas as avaliações")
f1, f2 = st.columns(2)
filtro_sent = f1.multiselect("Sentimento", ["positivo", "neutro", "negativo"], placeholder="Todos")
filtro_tema = f2.multiselect("Tema", TEMAS, placeholder="Todos")
tabela = dados
if filtro_sent:
    tabela = tabela[tabela["sentimento"].isin(filtro_sent)]
if filtro_tema:
    tabela = tabela[tabela["tema"].isin(filtro_tema)]
st.dataframe(
    tabela[["data", "cliente", "nota", "sentimento", "tema", "urgente", "resumo", "comentario"]],
    hide_index=True,
    use_container_width=True,
)
st.download_button(
    "⬇️ Baixar CSV classificado",
    dados.to_csv(index=False).encode("utf-8-sig"),
    file_name="avaliacoes_classificadas.csv",
    mime="text/csv",
)

# --- Respostas sugeridas
st.subheader("💬 Responder avaliações negativas")
for r in negativas.itertuples():
    with st.expander(f"{r.cliente} · nota {r.nota} · {r.resumo}"):
        st.write(f"> {r.comentario}")
        chave = f"resp_{r.id}"
        if st.button("Sugerir resposta", key=f"btn_{r.id}"):
            with st.spinner("Escrevendo resposta..."):
                try:
                    st.session_state[chave] = sugerir_resposta(r.cliente, r.nota, r.comentario)
                except Exception as e:
                    st.error(f"Não foi possível gerar a resposta: {e}")
        if chave in st.session_state:
            st.text_area("Resposta sugerida (edite antes de enviar)", st.session_state[chave], key=f"txt_{r.id}")
