"""
Envio de mensagens via Evolution API e as notificações internas (pro dono/consultor)
que o robô dispara em momentos-chave: lead quente, pedido fechado, pedido de
privacidade (LGPD), e transferência para atendimento humano.

NÃO envia a resposta ao CLIENTE - isso é feito pelo n8n (ver README do projeto),
evitando mandar a mesma mensagem duas vezes.
"""
import time

import requests
from psycopg2.extras import RealDictCursor

from app.config import logger, EVOLUTION_URL, EVOLUTION_KEY, PROPRIETARIO, CONSULTOR_TELEFONE
from app.database import get_db, release_db

# Códigos de status que valem a pena tentar de novo (erro do lado do servidor,
# ou "muitas requisições" - provavelmente vai passar sozinho em alguns segundos).
# 400/401/403/404 NÃO entram aqui de propósito: são erros PERMANENTES (chave errada,
# número inválido, endpoint errado) - tentar de novo não muda nada, só atrasa e
# desperdiça tempo. Nesses casos, falha rápido e loga bem para investigar depois.
_STATUS_TRANSIENTE = {429, 500, 502, 503, 504}


def enviar_whatsapp(telefone, mensagem, instance="automacao", tentativas=3):
    """Usado SOMENTE para as notificações internas abaixo (dono/consultor).
    A resposta ao cliente é enviada pelo n8n (não duplicar aqui).

    Tenta até `tentativas` vezes com espera crescente (1s, 2s, 4s...) SÓ para
    erros transientes (rede instável, servidor sobrecarregado). Erros permanentes
    (chave errada, dado inválido) não são repetidos - falham na primeira tentativa."""
    url = f"{EVOLUTION_URL}/message/sendText/{instance}"
    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_KEY}
    payload = {"number": telefone, "text": mensagem}

    for tentativa in range(1, tentativas + 1):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code < 300:
                logger.info(
                    "WhatsApp enviado com sucesso",
                    extra={"evento": "whatsapp_enviado", "telefone": telefone, "tentativa": tentativa},
                )
                return True
            if r.status_code in _STATUS_TRANSIENTE and tentativa < tentativas:
                espera = 2 ** (tentativa - 1)  # 1s, 2s, 4s...
                logger.warning(
                    f"Evolution API respondeu {r.status_code} (transiente) - tentando de novo em {espera}s",
                    extra={"evento": "whatsapp_retry", "status": r.status_code, "tentativa": tentativa},
                )
                time.sleep(espera)
                continue
            # Erro permanente, ou última tentativa transiente esgotada - desiste e loga.
            logger.error(
                f"Falha ao enviar WhatsApp (status {r.status_code}): {r.text[:200]}",
                extra={"evento": "whatsapp_falhou", "status": r.status_code, "tentativa": tentativa},
            )
            return False
        except requests.exceptions.RequestException as e:
            # Erro de rede/timeout - sempre vale tentar de novo (é o caso mais transiente que existe).
            if tentativa < tentativas:
                espera = 2 ** (tentativa - 1)
                logger.warning(
                    f"Erro de rede ao enviar WhatsApp - tentando de novo em {espera}s: {e}",
                    extra={"evento": "whatsapp_retry_rede", "tentativa": tentativa},
                )
                time.sleep(espera)
                continue
            logger.error(
                f"Falha ao enviar WhatsApp após {tentativas} tentativas (erro de rede): {e}",
                extra={"evento": "whatsapp_falhou_rede", "tentativas": tentativas},
            )
            return False
    return False


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
