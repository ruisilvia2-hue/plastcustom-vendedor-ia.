"""
Envio de mensagens via Evolution API, recuperação segura de mídia recebida e as
notificações internas (pro dono/consultor) que o robô dispara em momentos-chave:
lead quente, pedido fechado, pedido de privacidade (LGPD), e transferência para
atendimento humano.

NÃO envia a resposta normal ao CLIENTE - isso é feito pelo n8n (ver README do
projeto), evitando mandar a mesma mensagem duas vezes.
"""
import base64
import time
from typing import Any, Dict, Optional

import requests
from psycopg2.extras import RealDictCursor

from app.config import logger, EVOLUTION_URL, EVOLUTION_KEY, PROPRIETARIO, CONSULTOR_TELEFONE
from app.database import get_db, release_db, salvar_mensagem

# Códigos de status que valem a pena tentar de novo (erro do lado do servidor,
# ou "muitas requisições" - provavelmente vai passar sozinho em alguns segundos).
_STATUS_TRANSIENTE = {429, 500, 502, 503, 504}

# Claude Vision aceita estes formatos diretamente. Outros tipos de mídia (áudio,
# PDF, figurinha etc.) serão tratados em etapas próprias, sem fingir que são imagem.
_MIMES_IMAGEM_SUPORTADOS = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}

# Limite defensivo para não carregar uma mídia enorme no backend/IA.
_MAX_IMAGEM_BYTES = 12 * 1024 * 1024  # 12 MB


def _normalizar_base64(valor: str) -> str:
    """Remove prefixo data:*;base64, se a Evolution devolver nesse formato."""
    if not valor:
        return ""
    valor = valor.strip()
    if valor.startswith("data:") and "," in valor:
        valor = valor.split(",", 1)[1]
    return valor


def obter_midia_base64(
    instance: str,
    mensagem_evolution: Dict[str, Any],
    tentativas: int = 2,
) -> Dict[str, Any]:
    """Baixa uma mídia recebida do WhatsApp através da própria Evolution API.

    A Evolution expõe POST /chat/getBase64FromMediaMessage/{instance} e espera
    o WebMessageInfo completo em {"message": ...}. O retorno normalmente inclui
    mimetype, mediaType, fileName, caption e base64.

    Esta função NÃO chama IA e NÃO envia mensagem ao cliente. Ela apenas recupera,
    valida e normaliza a mídia para o webhook/ia.py consumirem depois.
    """
    if not instance:
        return {"ok": False, "erro": "instance_ausente"}

    if not isinstance(mensagem_evolution, dict) or not mensagem_evolution:
        return {"ok": False, "erro": "mensagem_evolution_invalida"}

    url = f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{instance}"
    headers = {
        "Content-Type": "application/json",
        "apikey": EVOLUTION_KEY,
    }
    payload = {
        "message": mensagem_evolution,
        "convertToMp4": False,
    }

    for tentativa in range(1, tentativas + 1):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=20)

            if r.status_code >= 300:
                if r.status_code in _STATUS_TRANSIENTE and tentativa < tentativas:
                    espera = 2 ** (tentativa - 1)
                    logger.warning(
                        "Falha transitória ao obter mídia da Evolution; tentando novamente",
                        extra={
                            "evento": "midia_retry",
                            "status": r.status_code,
                            "tentativa": tentativa,
                        },
                    )
                    time.sleep(espera)
                    continue

                logger.warning(
                    "Evolution não conseguiu devolver a mídia",
                    extra={
                        "evento": "midia_evolution_falhou",
                        "status": r.status_code,
                        "detalhe": r.text[:300],
                    },
                )
                return {
                    "ok": False,
                    "erro": "evolution_nao_obteve_midia",
                    "status": r.status_code,
                }

            dados = r.json() if r.content else {}
            b64 = _normalizar_base64(dados.get("base64") or "")
            mimetype = (dados.get("mimetype") or "").split(";", 1)[0].strip().lower()
            media_type = dados.get("mediaType")
            caption = dados.get("caption") or ""
            file_name = dados.get("fileName")

            if not b64:
                return {
                    "ok": False,
                    "erro": "midia_sem_base64",
                    "media_type": media_type,
                    "mimetype": mimetype,
                }

            try:
                tamanho_bytes = len(base64.b64decode(b64, validate=False))
            except Exception:
                return {
                    "ok": False,
                    "erro": "base64_invalido",
                    "media_type": media_type,
                    "mimetype": mimetype,
                }

            if tamanho_bytes > _MAX_IMAGEM_BYTES:
                logger.warning(
                    "Mídia recusada por tamanho",
                    extra={
                        "evento": "midia_grande_demais",
                        "bytes": tamanho_bytes,
                        "mimetype": mimetype,
                    },
                )
                return {
                    "ok": False,
                    "erro": "midia_grande_demais",
                    "tamanho_bytes": tamanho_bytes,
                    "limite_bytes": _MAX_IMAGEM_BYTES,
                    "mimetype": mimetype,
                }

            return {
                "ok": True,
                "base64": b64,
                "mimetype": mimetype,
                "media_type": media_type,
                "file_name": file_name,
                "caption": caption,
                "tamanho_bytes": tamanho_bytes,
                "imagem_suportada": mimetype in _MIMES_IMAGEM_SUPORTADOS,
            }

        except requests.exceptions.RequestException as e:
            if tentativa < tentativas:
                espera = 2 ** (tentativa - 1)
                logger.warning(
                    "Erro de rede ao obter mídia; tentando novamente",
                    extra={"evento": "midia_retry_rede", "tentativa": tentativa},
                )
                time.sleep(espera)
                continue

            logger.error(
                "Falha de rede ao obter mídia da Evolution",
                extra={"evento": "midia_falhou_rede", "erro": str(e)},
            )
            return {"ok": False, "erro": "falha_rede_evolution"}

        except ValueError as e:
            logger.warning(
                "Evolution devolveu resposta não-JSON ao solicitar mídia",
                extra={"evento": "midia_resposta_invalida", "erro": str(e)},
            )
            return {"ok": False, "erro": "resposta_evolution_invalida"}

    return {"ok": False, "erro": "falha_midia_desconhecida"}


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
                espera = 2 ** (tentativa - 1)
                logger.warning(
                    f"Evolution API respondeu {r.status_code} (transiente) - tentando de novo em {espera}s",
                    extra={"evento": "whatsapp_retry", "status": r.status_code, "tentativa": tentativa},
                )
                time.sleep(espera)
                continue
            logger.error(
                f"Falha ao enviar WhatsApp (status {r.status_code}): {r.text[:200]}",
                extra={"evento": "whatsapp_falhou", "status": r.status_code, "tentativa": tentativa},
            )
            return False
        except requests.exceptions.RequestException as e:
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


def reengajar_cliente(telefone: str, nome: Optional[str], conversa_id: str, cliente_id: str) -> bool:
    """Manda UMA mensagem curta de retomada pra um cliente que sumiu no meio da conversa."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='reengajamento'",
        (conversa_id,)
    )
    if cur.fetchone():
        cur.close(); release_db(db)
        return False

    primeiro_nome = nome.split(" ")[0] if nome else None
    saudacao = f"Oi, {primeiro_nome}! " if primeiro_nome else "Oi! "
    msg = saudacao + "Passando pra saber se ainda tá por aí 😊 Fico à disposição pra fechar seu orçamento quando quiser, é só me chamar!"

    ok = enviar_whatsapp(telefone, msg)
    if ok:
        cur.execute(
            "INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'reengajamento')",
            (cliente_id, conversa_id)
        )
        db.commit()
        salvar_mensagem(conversa_id, "ia", msg)
    cur.close(); release_db(db)
    return ok


def notificar_proprietario(cliente, score, conversa_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id FROM notificacoes WHERE cliente_id=%s AND tipo='lead_quente' AND enviada_em > NOW() - INTERVAL '24 hours'",
        (cliente["id"],)
    )
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    msg = f"LEAD QUENTE PLASTCUSTOM\n\nCliente: {nome}\nTelefone: +{cliente['telefone']}\nScore: {score}%\n\nCliente pronto para fechar! Entre em contato agora."
    enviar_whatsapp(PROPRIETARIO, msg)
    cur.execute(
        "INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'lead_quente')",
        (cliente["id"], conversa_id)
    )
    db.commit(); cur.close(); release_db(db)


def notificar_privacidade(cliente, conversa_id, tipo, detalhe):
    """Avisa o responsável sobre um pedido relacionado a dados pessoais (LGPD)."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='privacidade' AND enviada_em > NOW() - INTERVAL '24 hours'",
        (conversa_id,)
    )
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    rotulo = {
        "acesso": "QUER VER OS DADOS",
        "correcao": "QUER CORRIGIR DADOS",
        "exclusao": "QUER EXCLUIR DADOS (LGPD)",
        "duvida": "DÚVIDA SOBRE PRIVACIDADE",
    }.get(tipo, tipo.upper())
    msg = (
        f"PEDIDO DE PRIVACIDADE - {rotulo}\n\n"
        f"Cliente: {nome}\n"
        f"Telefone: +{cliente['telefone']}\n\n"
        f"Detalhe: {detalhe}\n\n"
        "Trate esse pedido diretamente com o cliente (a LGPD pede resposta em prazo razoável)."
    )
    enviar_whatsapp(CONSULTOR_TELEFONE, msg)
    cur.execute(
        "INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'privacidade')",
        (cliente["id"], conversa_id)
    )
    db.commit(); cur.close(); release_db(db)


def notificar_transferencia(cliente, conversa_id, motivo):
    """Avisa o consultor que o robô precisa de um humano."""
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
    cur.execute(
        "INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'transferencia')",
        (cliente["id"], conversa_id)
    )
    db.commit(); cur.close(); release_db(db)


def notificar_pedido_fechado(cliente, conversa_id, resumo):
    """Envia o resumo do pedido para o consultor responsável."""
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
    cur.execute(
        "INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'pedido_fechado')",
        (cliente["id"], conversa_id)
    )
    db.commit(); cur.close(); release_db(db)
