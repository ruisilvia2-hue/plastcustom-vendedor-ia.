"""
Envio de mensagens via Evolution API e as notificações internas (pro dono/consultor)
que o robô dispara em momentos-chave: lead quente, pedido fechado, pedido de
privacidade (LGPD), e transferência para atendimento humano.

NÃO envia a resposta ao CLIENTE - isso é feito pelo n8n (ver README do projeto),
evitando mandar a mesma mensagem duas vezes.
"""
import requests
from psycopg2.extras import RealDictCursor

from app.config import logger, EVOLUTION_URL, EVOLUTION_KEY, PROPRIETARIO, CONSULTOR_TELEFONE
from app.database import get_db, release_db


def enviar_whatsapp(telefone, mensagem, instance="automacao"):
    """Usado SOMENTE para as notificações internas abaixo (dono/consultor).
    A resposta ao cliente é enviada pelo n8n (não duplicar aqui)."""
    url = f"{EVOLUTION_URL}/message/sendText/{instance}"
    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_KEY}
    # formato correto da Evolution API v2: "number" e "text" no nível raiz
    payload = {"number": telefone, "text": mensagem}
    logger.info(f"Enviando WhatsApp para {telefone} via {instance}")
    logger.info(f"URL de envio: {url}")
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"Resposta da Evolution API: status={r.status_code} corpo={r.text[:200]}")
    except Exception as e:
        logger.error(f"Erro ao enviar WhatsApp: {e}")


def notificar_proprietario(cliente, score, conversa_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM notificacoes WHERE cliente_id=%s AND tipo='lead_quente' AND enviada_em > NOW() - INTERVAL '24 hours'", (cliente["id"],))
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    msg = f"LEAD QUENTE PLASTCUSTOM\n\nCliente: {nome}\nTelefone: +{cliente['telefone']}\nScore: {score}%\n\nCliente pronto para fechar! Entre em contato agora."
    enviar_whatsapp(PROPRIETARIO, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'lead_quente')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); release_db(db)


def notificar_privacidade(cliente, conversa_id, tipo, detalhe):
    """Avisa o responsável sobre um pedido relacionado a dados pessoais (LGPD) - acesso,
    correção, exclusão ou dúvida. NÃO apaga nada automaticamente: pedidos de exclusão
    precisam ser tratados por um humano, com cuidado."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='privacidade' AND enviada_em > NOW() - INTERVAL '24 hours'",
        (conversa_id,)
    )
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    rotulo = {"acesso": "QUER VER OS DADOS", "correcao": "QUER CORRIGIR DADOS", "exclusao": "QUER EXCLUIR DADOS (LGPD)", "duvida": "DÚVIDA SOBRE PRIVACIDADE"}.get(tipo, tipo.upper())
    msg = (
        f"PEDIDO DE PRIVACIDADE - {rotulo}\n\n"
        f"Cliente: {nome}\n"
        f"Telefone: +{cliente['telefone']}\n\n"
        f"Detalhe: {detalhe}\n\n"
        "Trate esse pedido diretamente com o cliente (a LGPD pede resposta em prazo razoável)."
    )
    enviar_whatsapp(CONSULTOR_TELEFONE, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'privacidade')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); release_db(db)


def notificar_transferencia(cliente, conversa_id, motivo):
    """Avisa o consultor que o robô não conseguiu ajudar e precisa de um humano.
    Tem um intervalo de 2h entre avisos pra mesma conversa, pra não virar spam."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='transferencia' AND enviada_em > NOW() - INTERVAL '2 hours'",
        (conversa_id,)
    )
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    msg = (
        "CLIENTE PRECISA DE AJUDA HUMANA - PLASTCUSTOM\n\n"
        f"Cliente: {nome}\n"
        f"Telefone: +{cliente['telefone']}\n\n"
        f"Motivo: {motivo}\n\n"
        "O robô já avisou o cliente que um consultor vai assumir a conversa."
    )
    enviar_whatsapp(CONSULTOR_TELEFONE, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'transferencia')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); release_db(db)


def notificar_pedido_fechado(cliente, conversa_id, resumo):
    """Envia o resumo do pedido (escrito pela própria IA) para o CONSULTOR_TELEFONE.
    Tem um intervalo curto (5 min) só pra evitar notificação duplicada instantânea -
    mas NÃO bloqueia pedidos novos/diferentes feitos depois, na mesma conversa."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='pedido_fechado' AND enviada_em > NOW() - INTERVAL '5 minutes'",
        (conversa_id,)
    )
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    msg = (
        "PEDIDO FECHADO - PLASTCUSTOM\n\n"
        f"Cliente: {nome}\n"
        f"Telefone: +{cliente['telefone']}\n\n"
        f"{resumo}\n\n"
        "Entre em contato para finalizar!"
    )
    enviar_whatsapp(CONSULTOR_TELEFONE, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'pedido_fechado')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); release_db(db)
