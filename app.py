import os, re, json, math
import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
DATABASE_URL = os.environ["DATABASE_URL"]
EVOLUTION_URL = os.environ["EVOLUTION_API_URL"]
EVOLUTION_KEY = os.environ["EVOLUTION_API_KEY"]
PROPRIETARIO = os.environ["PROPRIETARIO_TELEFONE"]
CONSULTOR_TELEFONE = os.environ["CONSULTOR_TELEFONE"]  # recebe o resumo automático quando um pedido é fechado
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]  # segredo compartilhado com o n8n para autenticar o /webhook

# ============================================================
# TABELA DE PREÇOS OFICIAL — portada da calculadora HTML da Plastcustom
# (Plastcustom_Orcamento.html — atualizada em 23/06/2026)
# Faixas: v1 = 150-200kg | v2 = 210-400kg | v3 = 410kg ou +
# ============================================================
TABELA = [
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 36.66, "v2": 36.01, "v3": 34.71},
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 37.83, "v2": 37.31, "v3": 36.01},
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 39.13, "v2": 38.48, "v3": 37.31},
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 40.95, "v2": 40.30, "v3": 39.78},

    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 39.78, "v2": 39.13, "v3": 37.96},
    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 40.95, "v2": 40.43, "v3": 39.13},
    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 42.25, "v2": 41.60, "v3": 40.43},
    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 44.07, "v2": 43.55, "v3": 42.90},

    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 33.15, "v2": 32.50, "v3": 31.20},
    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 34.58, "v2": 33.80, "v3": 32.50},
    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 35.23, "v2": 34.58, "v3": 33.15},
    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 37.18, "v2": 36.53, "v3": 35.88},

    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 26.13, "v2": 26.13, "v3": 26.13},
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 27.43, "v2": 27.43, "v3": 27.43},
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 28.08, "v2": 28.08, "v3": 28.08},
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 30.03, "v2": 30.03, "v3": 30.03},
]

PRECOS_PP = {
    "com_nf": [
        {"ate": 200,   "frente2": 30.00, "frente3": 31.50, "verso2": 32.00, "verso3": 33.50},
        {"ate": 400,   "frente2": 29.50, "frente3": 31.00, "verso2": 31.50, "verso3": 33.00},
        {"ate": 99999, "frente2": 28.50, "frente3": 30.00, "verso2": 30.50, "verso3": 32.00},
    ],
    "sem_nf": [
        {"ate": 200,   "frente2": 27.30, "frente3": 28.70, "verso2": 29.12, "verso3": 30.49},
        {"ate": 400,   "frente2": 26.85, "frente3": 28.21, "verso2": 28.67, "verso3": 30.03},
        {"ate": 99999, "frente2": 25.94, "frente3": 27.30, "verso2": 27.76, "verso3": 29.12},
    ]
}

MATERIAIS_VALIDOS = ["Virgem BD", "Virgem AD", "Reciclado Cor", "Reciclado Sem Cor", "Polipropileno (PP)"]
PRODUTOS_VALIDOS = ["Sacola Camiseta", "Sacola Vazada", "Saco Impresso Solda Fundo", "Saco com Aba"]

# Espessuras oficiais por produto (mm) — cada produto tem sua própria faixa
ESPESSURAS_POR_PRODUTO = {
    "Sacola Camiseta": [0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.028, 0.035, 0.045],
    "Sacola Vazada": [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014, 0.045],
    "Saco Impresso Solda Fundo": [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014, 0.045],
    "Saco com Aba": [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014, 0.045],
}

def espessura_mais_proxima(valor, produto=None):
    """Ajusta qualquer valor informado para a opção oficial mais próxima DENTRO do produto escolhido."""
    opcoes = ESPESSURAS_POR_PRODUTO.get(produto) or sorted({e for lst in ESPESSURAS_POR_PRODUTO.values() for e in lst})
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return opcoes[0]
    return min(opcoes, key=lambda x: abs(x - v))

def lookup_pp(imp, cores_faixa, kg, tipo_nota):
    tabela = PRECOS_PP.get(tipo_nota, PRECOS_PP["com_nf"])
    faixa = next((f for f in tabela if kg <= f["ate"]), tabela[-1])
    frente_verso = "VERSO" in imp
    ate2 = cores_faixa != "3 ou + cores"
    if frente_verso:
        return faixa["verso2"] if ate2 else faixa["verso3"]
    return faixa["frente2"] if ate2 else faixa["frente3"]

def lookup_fator_kg(material, imp, cores_faixa, kg, tipo_nota="com_nf"):
    if material == "Polipropileno (PP)":
        return lookup_pp(imp, cores_faixa, kg, tipo_nota)
    row = next((r for r in TABELA if r["m"] == material and r["i"] == imp and r["c"] == cores_faixa), None)
    if not row:
        return 0
    fator_base = row["v1"] if kg <= 200 else (row["v2"] if kg <= 400 else row["v3"])
    return round(fator_base * 0.91, 2) if tipo_nota == "sem_nf" else fator_base

def calcular_pedido_minimo(largura, altura, espessura, cores_n):
    """Pedido mínimo real: 150kg com impressão / 100kg sem impressão, convertido em milheiros
    de acordo com o peso de CADA combinação de tamanho+espessura (não é um número fixo)."""
    L = float(largura); A = float(altura); E = float(espessura)
    p_mil_kg = L * A * E
    if p_mil_kg <= 0:
        return None
    pedido_min_kg = 100 if int(cores_n) == 0 else 150
    unidades_min = math.ceil((pedido_min_kg / p_mil_kg) * 1000 / 500) * 500
    return {
        "milheiros_min": unidades_min / 1000,
        "unidades_min": unidades_min,
        "kg_min": pedido_min_kg,
    }

def calcular_preco(produto, material, largura, altura, cores_n, imp, milheiros, espessura=0.028, tipo_nota="com_nf"):
    """
    Calcula o preço EXATO seguindo a mesma lógica da calculadora oficial da Plastcustom.
    Não inclui clichê (cobrado à parte, conforme já informado pelo robô ao cliente).
    """
    L = float(largura)
    A = float(altura)
    E = float(espessura)
    MILH = float(milheiros)
    cores_n = int(cores_n)

    area = L * A
    vol = area * E
    p_un_g = vol            # peso por unidade (g) — mesma fórmula da calculadora
    p_mil_kg = p_un_g       # "peso do milheiro" no sentido usado pela calculadora
    total_kg = p_mil_kg * MILH

    cores_faixa = "até 2 cores" if cores_n <= 2 else "3 ou + cores"
    preco_kg = lookup_fator_kg(material, imp, cores_faixa, total_kg, tipo_nota)

    if preco_kg <= 0:
        raise ValueError(f"Combinação sem preço na tabela: {material} / {imp} / {cores_faixa}")

    # Sem impressão: desconto de R$2 no fator kg (regra da calculadora), exceto PP
    if cores_n == 0 and material != "Polipropileno (PP)":
        preco_kg -= 2

    mil_base = preco_kg * p_mil_kg

    # Regra: milheiro < 1,5kg soma R$3,00 no fator kg
    adicional_fator_kg = 3 if 0 < p_mil_kg < 1.5 else 0
    adicional_mil = adicional_fator_kg * p_mil_kg

    mil = mil_base + adicional_mil
    unitario = mil / 1000
    total = mil * MILH

    pedido_min_kg = 100 if cores_n == 0 else 150
    minimo = calcular_pedido_minimo(L, A, E, cores_n)

    return {
        "preco_kg": round(preco_kg, 2),
        "unitario": round(unitario, 4),
        "milheiro": round(mil, 2),
        "total": round(total, 2),
        "peso_total_kg": round(total_kg, 2),
        "espessura_usada": round(E, 3),
        "pedido_minimo_kg": pedido_min_kg,
        "pedido_minimo_milheiros": minimo["milheiros_min"] if minimo else None,
        "atende_minimo": total_kg >= pedido_min_kg,
    }

def extrair_pedido(hist_txt, mensagem_atual):
    """Usa a IA apenas para EXTRAIR dados estruturados da conversa (não para calcular preço)."""
    tabela_esp_txt = "\n".join(
        f"  {produto}: " + ", ".join(f"{e:.3f}".replace(".", ",") for e in lista)
        for produto, lista in ESPESSURAS_POR_PRODUTO.items()
    )
    prompt_extracao = f"""Baseado nesta conversa entre um vendedor e um cliente, extraia os dados do pedido.
Responda APENAS com um JSON válido, sem texto antes ou depois, sem markdown.

Conversa:
{hist_txt}
Cliente (última mensagem): {mensagem_atual}

Formato exato de resposta:
{{
  "produto": "Sacola Camiseta" ou "Sacola Vazada" ou "Saco Impresso Solda Fundo" ou "Saco com Aba" ou null,
  "material": "Virgem BD" ou "Virgem AD" ou "Reciclado Cor" ou "Reciclado Sem Cor" ou "Polipropileno (PP)" ou null,
  "largura": numero ou null,
  "altura": numero ou null,
  "espessura": numero ou null,
  "cores_n": numero ou null,
  "impressao": "FRENTE" ou "FRENTE_VERSO" ou null,
  "milheiros": numero ou null,
  "completo": true ou false
}}

Regras:
- "material": se o cliente não mencionou, use "Virgem BD" (é o padrão da empresa)
- "espessura": cada produto tem sua PRÓPRIA lista de espessuras oficiais (em mm):
{tabela_esp_txt}
  Extraia o valor que o cliente escolheu, considerando a lista do produto já identificado. Se não foi perguntado/respondido ainda, deixe null.
- "cores_n": use 0 se o cliente disse que não quer impressão/logo; use o número de cores se ele informou
- "impressao": "FRENTE" se nada foi dito sobre frente e verso
- "largura"/"altura": converta o tamanho informado (ex: "40x50") em dois números
- "completo": true SOMENTE se produto, largura, altura, espessura, cores_n e milheiros estiverem TODOS preenchidos (não nulos)
"""
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt_extracao}]
        )
        texto = response.content[0].text.strip()
        texto = re.sub(r'^```json\s*|\s*```$', '', texto).strip()
        return json.loads(texto)
    except Exception as e:
        print(f"Erro ao extrair pedido: {e}")
        return {"completo": False}

SYSTEM_PROMPT = """Você é o Rui, vendedor de alta performance da Plastcustom. Conhece cada detalhe dos produtos e fecha vendas com naturalidade. Nunca mencione catálogo, sistema ou virtual.

PRODUTOS:
1. Sacola Camiseta - alça integrada no corpo
2. Sacola Vazada - alça recortada no plástico
3. Saco Impresso Solda Fundo - saco liso com solda no fundo
4. Saco com Aba - saco com dobra superior

TAMANHOS: 30x40 / 40x50 / 50x60 / 60x80 / 80x100 cm
MATERIAIS: Virgem BD (padrão) / Virgem AD (resistente) / PP (transparente) / Reciclado
ESPESSURAS DISPONÍVEIS (mm) — cada produto tem sua própria faixa, pergunte a espessura SOMENTE depois de saber o produto:
  - Sacola Camiseta: 0,003 / 0,004 / 0,005 / 0,006 / 0,007 / 0,008 / 0,009 / 0,028 / 0,035 / 0,045
  - Sacola Vazada, Saco Impresso Solda Fundo, Saco com Aba: 0,004 / 0,005 / 0,006 / 0,007 / 0,008 / 0,009 / 0,010 / 0,011 / 0,012 / 0,013 / 0,014 / 0,045
  - Se o cliente não souber qual escolher, explique rapidamente: quanto maior o número, mais grossa/resistente a sacola.
IMPRESSÃO: até 6 cores, frente e/ou verso. Clichê cobrado à parte na primeira compra.

COMO APRESENTAR AS OPÇÕES — MUITO IMPORTANTE:
- Sempre que for perguntar produto, material, tamanho, espessura ou número de cores, apresente as opções
  em formato de lista curta (menu), para o cliente só escolher — não faça pergunta totalmente aberta.
- Ao perguntar a espessura, use APENAS a lista de espessuras do produto que o cliente já escolheu (nunca ofereça
  um valor que não esteja na lista daquele produto específico). Se o cliente pedir um valor fora da lista,
  explique que não está disponível para aquele produto e mostre de novo as opções válidas dele.

REGRA DE PREÇO — MUITO IMPORTANTE:
- Você NUNCA calcula ou estima preço por conta própria, nem "proporcionalmente".
- Quando o contexto desta mensagem trouxer um bloco "DADOS CALCULADOS OFICIAIS", use EXATAMENTE
  esses valores na sua resposta (não arredonde diferente, não invente outro número).
- Se esse bloco não estiver presente e o cliente perguntar o preço, NÃO informe nenhum valor.
  Diga que precisa confirmar mais alguns detalhes (produto, tamanho, quantidade em mil, impressão)
  antes de calcular certinho.
- Se aparecer um aviso de erro no cálculo, diga que vai confirmar o valor exato com a equipe e
  retornar em breve — nunca invente um número nessa situação.

CONDIÇÕES:
- Pedido mínimo: NÃO é um número fixo — varia por produto, tamanho e espessura (é sempre baseado em peso:
  100kg sem impressão / 150kg com impressão). Quando o contexto trouxer "PEDIDO MÍNIMO PARA ESTA COMBINAÇÃO",
  use EXATAMENTE esse valor em mil unidades. NUNCA diga "30 mil" de forma genérica — cada combinação tem seu próprio mínimo.
- Prazo: 30 a 40 dias úteis após aprovação da arte
- Frete: FOB Curitiba-PR ou CIF negociado
- Pagamento: 28 dias ou 28/56 dias
- Validade da proposta: 7 dias

FLUXO DE VENDA:
1. Cumprimente e pergunte o tipo de negócio
2. Pergunte qual produto precisa (apresente as opções)
3. Pergunte o tamanho (apresente as opções)
4. Pergunte o material (apresente as opções: Virgem BD - padrão / Virgem AD - resistente / PP - transparente / Reciclado)
5. Pergunte a espessura, usando a lista específica do produto já escolhido, explicando as faixas e sugerindo com base no uso do cliente
6. Pergunte sobre impressão, número de cores e logo (isso precisa vir ANTES da quantidade, pois o pedido mínimo depende de ter ou não impressão)
7. Pergunte a quantidade em MIL unidades — quando o contexto trouxer o pedido mínimo calculado para essa combinação, informe esse valor específico (nunca um número fixo genérico)
8. Quando tiver os DADOS CALCULADOS OFICIAIS, apresente o preço com confiança
9. Feche: Posso gerar a proposta para você?
10. Quando confirmar diga: Perfeito! Estou passando seus dados para nosso consultor finalizar. Em breve entrarão em contato!

OBJEÇÕES:
- Tá caro: mostre custo por unidade e sugira quantidade maior
- Vou pensar: Posso segurar esse preço por 7 dias
- Pouco: explique mínimo de 30 mil

REGRAS:
- Uma pergunta por vez
- Máximo 3 parágrafos
- Tom confiante direto e profissional"""

SINAIS = {
    "perguntou_preco": (["preço","valor","custa","quanto","tabela"], 20),
    "perguntou_prazo": (["prazo","entrega","quando","dias"], 15),
    "escolheu_modelo": (["camiseta","vazada","impresso","aba","sacola","saco"], 25),
    "escolheu_tamanho": (["30x40","40x50","50x60","60x80","80x100","tamanho","medida"], 20),
    "escolheu_quantidade": (["mil","unidades","quantidade"], 30),
    "pediu_orcamento": (["orçamento","proposta","cotação","calcul"], 35),
    "tem_empresa": (["empresa","loja","mercado","farmácia","padaria","cnpj","supermercado"], 15),
    "mandou_logo": (["logo","logomarca","arquivo","arte"], 40),
    "confirmou_pedido": (["confirmo","quero fechar","fechado","pode gerar","vamos","sim pode"], 50),
    "vou_pensar": (["pensar","depois","talvez","não sei"], -10),
    "ta_caro": (["caro","salgado","muito caro"], -15),
}

def limpar_telefone(telefone):
    return re.sub(r'[^0-9]', '', telefone)[:20]

def get_db():
    return psycopg2.connect(DATABASE_URL)

def buscar_ou_criar_cliente(telefone):
    telefone = limpar_telefone(telefone)
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM clientes WHERE telefone=%s", (telefone,))
    c = cur.fetchone()
    if not c:
        cur.execute("INSERT INTO clientes (telefone) VALUES (%s) RETURNING *", (telefone,))
        c = cur.fetchone()
        db.commit()
    cur.close(); db.close()
    return dict(c)

def buscar_ou_criar_conversa(cliente_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM conversas WHERE cliente_id=%s AND status='ativa' ORDER BY inicio DESC LIMIT 1", (cliente_id,))
    c = cur.fetchone()
    if not c:
        cur.execute("INSERT INTO conversas (cliente_id) VALUES (%s) RETURNING *", (cliente_id,))
        c = cur.fetchone()
        db.commit()
    cur.close(); db.close()
    return dict(c)

def salvar_mensagem(conversa_id, remetente, conteudo):
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO mensagens (conversa_id, remetente, conteudo) VALUES (%s,%s,%s)", (conversa_id, remetente, conteudo))
    cur.execute("UPDATE conversas SET ultima_mensagem=NOW() WHERE id=%s", (conversa_id,))
    db.commit(); cur.close(); db.close()

def obter_historico(conversa_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT remetente, conteudo FROM mensagens WHERE conversa_id=%s ORDER BY timestamp DESC LIMIT 30", (conversa_id,))
    msgs = list(reversed(cur.fetchall()))
    cur.close(); db.close()
    return msgs

def calcular_score(conversa_id, cliente_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT conteudo FROM mensagens WHERE conversa_id=%s AND remetente='cliente'", (conversa_id,))
    msgs = cur.fetchall()
    score = 0
    detectados = set()
    for msg in msgs:
        texto = msg["conteudo"].lower()
        for sinal, (keywords, pts) in SINAIS.items():
            if sinal not in detectados and any(k in texto for k in keywords):
                score += pts
                detectados.add(sinal)
    score = max(0, min(100, score))
    categoria = "quente" if score >= 80 else "morno" if score >= 50 else "frio"
    cur.execute("INSERT INTO leads (cliente_id, conversa_id, score, categoria) VALUES (%s,%s,%s,%s)", (cliente_id, conversa_id, score, categoria))
    cur.execute("UPDATE conversas SET lead_score=%s WHERE id=%s", (score, conversa_id))
    db.commit(); cur.close(); db.close()
    return {"score": score, "categoria": categoria}

def enviar_whatsapp(telefone, mensagem, instance="automacao"):
    """
    Usado SOMENTE para notificar o proprietário sobre leads quentes.
    A resposta ao cliente é enviada pelo n8n (não duplicar aqui).
    """
    url = f"{EVOLUTION_URL}/message/sendText/{instance}"
    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_KEY}
    # formato correto da Evolution API v2: "number" e "text" no nível raiz
    payload = {"number": telefone, "text": mensagem}
    print(f"Enviando para {telefone} via {instance}")
    print(f"URL: {url}")
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"Status: {r.status_code} | Resposta: {r.text[:200]}")
    except Exception as e:
        print(f"Erro ao enviar: {e}")

def notificar_proprietario(cliente, score, conversa_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM notificacoes WHERE cliente_id=%s AND tipo='lead_quente' AND enviada_em > NOW() - INTERVAL '24 hours'", (cliente["id"],))
    if cur.fetchone():
        cur.close(); db.close(); return
    nome = cliente.get("nome") or cliente["telefone"]
    msg = f"LEAD QUENTE PLASTCUSTOM\n\nCliente: {nome}\nTelefone: +{cliente['telefone']}\nScore: {score}%\n\nCliente pronto para fechar! Entre em contato agora."
    enviar_whatsapp(PROPRIETARIO, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'lead_quente')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); db.close()

def notificar_pedido_fechado(cliente, conversa_id, pedido, calc):
    """Envia o resumo do pedido para o CONSULTOR_TELEFONE assim que o cliente confirma o fechamento.
    Só dispara uma vez por conversa (evita reenviar se o cliente confirmar de novo)."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='pedido_fechado'", (conversa_id,))
    if cur.fetchone():
        cur.close(); db.close(); return
    nome = cliente.get("nome") or cliente["telefone"]
    imp_label = "frente e verso" if pedido.get("impressao") == "FRENTE_VERSO" else "frente"
    material = pedido.get("material") or "Virgem BD"
    msg = (
        "PEDIDO FECHADO - PLASTCUSTOM\n\n"
        f"Cliente: {nome}\n"
        f"Telefone: +{cliente['telefone']}\n\n"
        f"Produto: {pedido['produto']} {pedido['largura']}x{pedido['altura']}cm\n"
        f"Material: {material}\n"
        f"Espessura: {calc['espessura_usada']}mm\n"
        f"Impressao: {pedido['cores_n']} cores, {imp_label}\n"
        f"Quantidade: {pedido['milheiros']} mil unidades\n\n"
        f"Preco por milheiro: R$ {calc['milheiro']}\n"
        f"TOTAL: R$ {calc['total']}\n\n"
        "Entre em contato para finalizar!"
    )
    enviar_whatsapp(CONSULTOR_TELEFONE, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'pedido_fechado')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); db.close()

@app.route("/webhook", methods=["POST"])
def webhook():
    # Autenticação: só aceita chamadas que tragam o segredo combinado com o n8n.
    # Sem isso, qualquer pessoa na internet que descobrisse esse endereço poderia
    # gastar seus créditos de IA e mandar mensagens em nome do robô.
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"erro": "não autorizado"}), 401

    data = request.get_json()
    telefone_raw = data.get("telefone","").strip()
    mensagem = data.get("mensagem","").strip()[:2000]  # limite defensivo contra payloads abusivos
    instance = data.get("instance","automacao")
    if not telefone_raw or not mensagem:
        return jsonify({"erro": "dados incompletos"}), 400
    try:
        cliente = buscar_ou_criar_cliente(telefone_raw)
        conversa = buscar_ou_criar_conversa(cliente["id"])
        salvar_mensagem(conversa["id"], "cliente", mensagem)
        historico = obter_historico(conversa["id"])
        hist_txt = "\n".join([f"{'Cliente' if m['remetente']=='cliente' else 'Rui'}: {m['conteudo']}" for m in historico[:-1]])

        # === NOVO: extrai os dados do pedido e calcula o preço EXATO (sem achismo) ===
        dados_preco_txt = ""
        calc_resultado = None
        pedido = extrair_pedido(hist_txt, mensagem)

        # Assim que já soubermos produto+tamanho+espessura+cores, calculamos o MÍNIMO REAL
        # dessa combinação (não é fixo em 30 mil - varia por peso) para o robô já informar certo.
        campos_base = ["produto", "largura", "altura", "espessura", "cores_n"]
        if all(pedido.get(c) is not None for c in campos_base):
            try:
                espessura_base = espessura_mais_proxima(pedido.get("espessura"), pedido.get("produto"))
                minimo = calcular_pedido_minimo(pedido["largura"], pedido["altura"], espessura_base, pedido["cores_n"])
                if minimo:
                    dados_preco_txt += f"""

PEDIDO MÍNIMO PARA ESTA COMBINAÇÃO (produto/tamanho/espessura já escolhidos): {minimo['milheiros_min']:.1f} mil unidades ({minimo['kg_min']} kg mínimo).
NÃO diga "30 mil" de forma genérica - use exatamente este valor específico.
"""
            except Exception as e:
                print(f"Erro ao calcular pedido mínimo: {e}")

        if pedido.get("completo"):
            try:
                imp_map = "IMPRESSÃO FRENTE / VERSO" if pedido.get("impressao") == "FRENTE_VERSO" else "IMPRESSÃO FRENTE"
                material = pedido.get("material") or "Virgem BD"
                espessura = espessura_mais_proxima(pedido.get("espessura"), pedido.get("produto"))
                calc = calcular_preco(
                    produto=pedido["produto"],
                    material=material,
                    largura=pedido["largura"],
                    altura=pedido["altura"],
                    cores_n=pedido["cores_n"],
                    imp=imp_map,
                    milheiros=pedido["milheiros"],
                    espessura=espessura,
                )
                dados_preco_txt += f"""

DADOS CALCULADOS OFICIAIS (use EXATAMENTE estes valores, não calcule nada por conta própria):
Produto: {pedido['produto']} {pedido['largura']}x{pedido['altura']}cm, {material}, espessura {espessura:.3f}mm, {pedido['cores_n']} cores, {imp_map}
Preço por milheiro: R$ {calc['milheiro']}
Total ({pedido['milheiros']} mil unidades): R$ {calc['total']}
Peso total do pedido: {calc['peso_total_kg']} kg (mínimo exigido: {calc['pedido_minimo_kg']} kg)
"""
                if not calc["atende_minimo"]:
                    dados_preco_txt += "\nATENÇÃO: peso abaixo do mínimo exigido. Explique ao cliente que não é possível fechar nesse peso e sugira aumentar a quantidade.\n"
                else:
                    calc_resultado = calc
            except Exception as e:
                print(f"Erro ao calcular preço: {e}")
                dados_preco_txt = "\n\nAVISO: não foi possível calcular o preço automaticamente para esta combinação. NÃO informe nenhum valor - diga que vai confirmar com a equipe e retornar em breve.\n"

        prompt = f"Historico:\n{hist_txt}{dados_preco_txt}\n\nCliente: {mensagem}\nRui:"
        response = client.messages.create(model="claude-sonnet-4-6", max_tokens=600, system=SYSTEM_PROMPT, messages=[{"role":"user","content":prompt}])
        resposta = response.content[0].text.strip()
        salvar_mensagem(conversa["id"], "ia", resposta)
        lead = calcular_score(conversa["id"], cliente["id"])
        # NOTA: o envio da mensagem ao cliente é feito pelo n8n (HTTP Request1),
        # por isso NÃO chamamos enviar_whatsapp() aqui para o cliente (evita duplicar).
        if lead["score"] >= 80:
            notificar_proprietario(cliente, lead["score"], conversa["id"])

        # Pedido fechado: cliente confirmou e já temos o cálculo oficial -> avisa o consultor
        palavras_confirmacao = SINAIS["confirmou_pedido"][0]
        if calc_resultado and any(p in mensagem.lower() for p in palavras_confirmacao):
            notificar_pedido_fechado(cliente, conversa["id"], pedido, calc_resultado)

        return jsonify({"ok": True, "resposta": resposta, "score": lead["score"], "categoria": lead["categoria"]})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",3000)))
