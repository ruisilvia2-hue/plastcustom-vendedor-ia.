import os, re
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

SYSTEM_PROMPT = """Você é o Rui, vendedor de alta performance da Plastcustom. Conhece cada detalhe dos produtos e fecha vendas com naturalidade. Nunca mencione catálogo, sistema ou virtual.

PRODUTOS:
1. Sacola Camiseta - alça integrada no corpo
2. Sacola Vazada - alça recortada no plástico
3. Saco Impresso Solda Fundo - saco liso com solda no fundo
4. Saco com Aba - saco com dobra superior

TAMANHOS: 30x40 / 40x50 / 50x60 / 60x80 / 80x100 cm
MATERIAIS: Virgem BD (padrão) / Virgem AD (resistente) / PP (transparente) / Reciclado
IMPRESSÃO: até 6 cores, frente e/ou verso. Clichê cobrado à parte na primeira compra.

PREÇOS (R$ por MIL unidades):
Sacola Camiseta 40x50 Virgem BD: sem impressão R$580 / 1 cor R$680 / 2 cores R$780
Sacola Camiseta 50x60 Virgem BD: sem impressão R$760 / 1 cor R$860 / 2 cores R$960
Saco Impresso 30x40 Virgem AD: 1 cor R$980 / 2 cores frente+verso R$1.100
Para outros tamanhos calcule proporcionalmente e informe com confiança.

CONDIÇÕES:
- Pedido mínimo: 30 mil unidades
- Prazo: 10 a 20 dias úteis após aprovação da arte
- Frete: FOB Curitiba-PR ou CIF negociado
- Pagamento: 28 dias ou 28/56 dias
- Validade da proposta: 7 dias

FLUXO DE VENDA:
1. Cumprimente e pergunte o tipo de negócio
2. Pergunte qual produto precisa
3. Pergunte o tamanho
4. Pergunte a quantidade em MIL unidades
5. Pergunte sobre impressão e logo
6. Calcule e apresente o preço com confiança
7. Feche: Posso gerar a proposta para você?
8. Quando confirmar diga: Perfeito! Estou passando seus dados para nosso consultor finalizar. Em breve entrarão em contato!

OBJEÇÕES:
- Tá caro: mostre custo por unidade e sugira quantidade maior
- Vou pensar: Posso segurar esse preço por 7 dias
- Pouco: explique mínimo de 30 mil

REGRAS:
- Uma pergunta por vez
- Quando tiver produto+tamanho+quantidade+impressão calcule e apresente o preço
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
    url = f"{EVOLUTION_URL}/message/sendText/{instance}"
    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_KEY}
    payload = {"number": telefone, "options": {"delay": 1500, "presence": "composing"}, "textMessage": {"text": mensagem}}
    requests.post(url, json=payload, headers=headers, timeout=10)

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

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    telefone_raw = data.get("telefone","").strip()
    mensagem = data.get("mensagem","").strip()
    instance = data.get("instance","automacao")
    if not telefone_raw or not mensagem:
        return jsonify({"erro": "dados incompletos"}), 400
    try:
        cliente = buscar_ou_criar_cliente(telefone_raw)
        conversa = buscar_ou_criar_conversa(cliente["id"])
        salvar_mensagem(conversa["id"], "cliente", mensagem)
        historico = obter_historico(conversa["id"])
        hist_txt = "\n".join([f"{'Cliente' if m['remetente']=='cliente' else 'Rui'}: {m['conteudo']}" for m in historico[:-1]])
        prompt = f"Historico:\n{hist_txt}\n\nCliente: {mensagem}\nRui:"
        response = client.messages.create(model="claude-sonnet-4-6", max_tokens=600, system=SYSTEM_PROMPT, messages=[{"role":"user","content":prompt}])
        resposta = response.content[0].text.strip()
        salvar_mensagem(conversa["id"], "ia", resposta)
        lead = calcular_score(conversa["id"], cliente["id"])
        enviar_whatsapp(telefone_raw, resposta, instance)
        if lead["score"] >= 80:
            notificar_proprietario(cliente, lead["score"], conversa["id"])
        return jsonify({"ok": True, "resposta": resposta, "score": lead["score"], "categoria": lead["categoria"]})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",3000)))
