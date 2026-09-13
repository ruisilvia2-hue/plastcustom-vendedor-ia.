import os
import re
import hashlib
import logging
from typing import Dict, Any

import redis


logger = logging.getLogger("vendedor_ia.antibot")

# Redis dedicado ao Anti-Bot
REDIS_URL = os.environ.get("ANTI_BOT_REDIS_URL", "").strip()

# Limites por número/remoteJid.
# Como o n8n já possui buffer, cada chamada aqui representa um "turno" de conversa,
# não necessariamente cada mensagem digitada pelo cliente.
LIMIT_10M = int(os.environ.get("ANTI_BOT_LIMIT_10M", "12"))
LIMIT_1H = int(os.environ.get("ANTI_BOT_LIMIT_1H", "30"))
LIMIT_24H = int(os.environ.get("ANTI_BOT_LIMIT_24H", "80"))

# Repetição idêntica ou praticamente idêntica.
REPEAT_LIMIT = int(os.environ.get("ANTI_BOT_REPEAT_LIMIT", "5"))
REPEAT_WINDOW_SECONDS = int(os.environ.get("ANTI_BOT_REPEAT_WINDOW", "600"))

# Reincidência e cooldown progressivo.
STRIKE_WINDOW_SECONDS = int(os.environ.get("ANTI_BOT_STRIKE_WINDOW", "604800"))  # 7 dias
COOLDOWN_FIRST = int(os.environ.get("ANTI_BOT_COOLDOWN_FIRST", "1800"))          # 30 min
COOLDOWN_SECOND = int(os.environ.get("ANTI_BOT_COOLDOWN_SECOND", "21600"))       # 6 h
COOLDOWN_THIRD = int(os.environ.get("ANTI_BOT_COOLDOWN_THIRD", "86400"))         # 24 h

# Modo mais seguro: se o Redis dedicado cair, o vendedor NÃO chama a IA.
# Se algum dia você preferir disponibilidade acima de proteção, pode definir:
# ANTI_BOT_FAIL_OPEN=true
FAIL_OPEN = os.environ.get("ANTI_BOT_FAIL_OPEN", "false").strip().lower() in {
    "1", "true", "yes", "sim", "on"
}

PREFIX = "plastcustom:antibot"

_redis_client = None

# INCR + EXPIRE de forma atômica.
# Isso evita a situação em que o contador sobe, mas fica sem expiração.
_LUA_INCR_WITH_TTL = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


def _get_redis():
    global _redis_client

    if _redis_client is not None:
        return _redis_client

    if not REDIS_URL:
        raise RuntimeError("ANTI_BOT_REDIS_URL não configurada")

    client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )
    client.ping()
    _redis_client = client
    logger.info("Anti-Bot conectado ao Redis dedicado")
    return _redis_client


def _normalizar_telefone(valor: str) -> str:
    """
    Transforma telefone/JID numa chave estável.
    Ex.: 5541999999999@s.whatsapp.net -> 5541999999999
    """
    bruto = str(valor or "").strip()
    digitos = re.sub(r"\D", "", bruto)

    if digitos:
        return digitos

    # Fallback: nunca deixa todos os remetentes vazios caírem na mesma chave.
    if bruto:
        return hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:24]

    return "sem-identificador"


def _normalizar_mensagem(texto: str) -> str:
    """
    Normalização simples para identificar loops/repetições sem depender
    de interpretação por IA.
    """
    texto = str(texto or "").lower().strip()
    texto = re.sub(r"https?://\S+", "<url>", texto)
    texto = re.sub(r"\s+", " ", texto)
    texto = re.sub(r"[^\wÀ-ÿ<> ]+", "", texto, flags=re.UNICODE)
    return texto[:1000]


def _incrementar(client, chave: str, ttl_seconds: int) -> int:
    return int(
        client.eval(
            _LUA_INCR_WITH_TTL,
            1,
            chave,
            int(ttl_seconds),
        )
    )


def _cooldown_por_reincidencia(strikes: int) -> int:
    if strikes <= 1:
        return COOLDOWN_FIRST
    if strikes == 2:
        return COOLDOWN_SECOND
    return COOLDOWN_THIRD


def verificar_antibot(telefone: str, mensagem: str) -> Dict[str, Any]:
    """
    Circuit breaker principal.

    Retorno quando permitido:
        {
          "allow_ai": True,
          "blocked": False,
          "counters": {...}
        }

    Retorno quando bloqueado:
        {
          "allow_ai": False,
          "blocked": True,
          "motivo": "...",
          "retry_after_seconds": 1800,
          "counters": {...}
        }

    IMPORTANTE:
    Esta função deve ser chamada ANTES de qualquer chamada à IA.
    """
    identidade = _normalizar_telefone(telefone)

    try:
        client = _get_redis()

        block_key = f"{PREFIX}:block:{identidade}"
        block_reason = client.get(block_key)

        if block_reason:
            ttl = int(client.ttl(block_key))
            logger.warning(
                "Anti-Bot bloqueou número já em cooldown telefone=%s ttl=%ss motivo=%s",
                identidade,
                ttl,
                block_reason,
            )
            return {
                "allow_ai": False,
                "blocked": True,
                "motivo": block_reason,
                "retry_after_seconds": max(ttl, 0),
                "counters": {},
            }

        # Contadores de volume.
        c10 = _incrementar(
            client,
            f"{PREFIX}:rate:10m:{identidade}",
            10 * 60,
        )
        c1h = _incrementar(
            client,
            f"{PREFIX}:rate:1h:{identidade}",
            60 * 60,
        )
        c24h = _incrementar(
            client,
            f"{PREFIX}:rate:24h:{identidade}",
            24 * 60 * 60,
        )

        counters = {
            "10m": c10,
            "1h": c1h,
            "24h": c24h,
        }

        # Detector de repetição determinístico.
        repeticoes = 0
        normalizada = _normalizar_mensagem(mensagem)

        if normalizada:
            fingerprint = hashlib.sha256(
                normalizada.encode("utf-8")
            ).hexdigest()[:24]

            repeticoes = _incrementar(
                client,
                f"{PREFIX}:repeat:{identidade}:{fingerprint}",
                REPEAT_WINDOW_SECONDS,
            )

        counters["repeticoes"] = repeticoes

        motivos = []

        if c10 > LIMIT_10M:
            motivos.append(f"limite_10m:{c10}>{LIMIT_10M}")

        if c1h > LIMIT_1H:
            motivos.append(f"limite_1h:{c1h}>{LIMIT_1H}")

        if c24h > LIMIT_24H:
            motivos.append(f"limite_24h:{c24h}>{LIMIT_24H}")

        if repeticoes > REPEAT_LIMIT:
            motivos.append(
                f"mensagem_repetida:{repeticoes}>{REPEAT_LIMIT}"
            )

        if motivos:
            strikes = _incrementar(
                client,
                f"{PREFIX}:strikes:{identidade}",
                STRIKE_WINDOW_SECONDS,
            )

            cooldown = _cooldown_por_reincidencia(strikes)
            motivo = "|".join(motivos)

            client.set(
                block_key,
                motivo,
                ex=cooldown,
            )

            logger.warning(
                "Anti-Bot ATIVADO telefone=%s strikes=%s cooldown=%ss motivo=%s counters=%s",
                identidade,
                strikes,
                cooldown,
                motivo,
                counters,
            )

            return {
                "allow_ai": False,
                "blocked": True,
                "motivo": motivo,
                "retry_after_seconds": cooldown,
                "strikes": strikes,
                "counters": counters,
            }

        return {
            "allow_ai": True,
            "blocked": False,
            "motivo": None,
            "retry_after_seconds": 0,
            "counters": counters,
        }

    except Exception as exc:
        logger.exception("Falha no Anti-Bot/Redis: %s", exc)

        if FAIL_OPEN:
            # Disponibilidade acima de proteção.
            return {
                "allow_ai": True,
                "blocked": False,
                "motivo": "redis_indisponivel_fail_open",
                "retry_after_seconds": 0,
                "counters": {},
                "redis_ok": False,
            }

        # Modo padrão e mais seguro: sem Redis, sem IA.
        return {
            "allow_ai": False,
            "blocked": True,
            "motivo": "redis_indisponivel_fail_closed",
            "retry_after_seconds": 60,
            "counters": {},
            "redis_ok": False,
        }


def obter_status_antibot(telefone: str) -> Dict[str, Any]:
    """
    Função auxiliar para diagnóstico futuro.
    Não é necessária no fluxo normal.
    """
    identidade = _normalizar_telefone(telefone)

    try:
        client = _get_redis()
        block_key = f"{PREFIX}:block:{identidade}"

        return {
            "telefone": identidade,
            "blocked": bool(client.exists(block_key)),
            "motivo": client.get(block_key),
            "retry_after_seconds": max(int(client.ttl(block_key)), 0),
            "count_10m": int(client.get(f"{PREFIX}:rate:10m:{identidade}") or 0),
            "count_1h": int(client.get(f"{PREFIX}:rate:1h:{identidade}") or 0),
            "count_24h": int(client.get(f"{PREFIX}:rate:24h:{identidade}") or 0),
            "strikes": int(client.get(f"{PREFIX}:strikes:{identidade}") or 0),
        }

    except Exception as exc:
        logger.exception("Erro consultando status do Anti-Bot: %s", exc)
        return {
            "telefone": identidade,
            "erro": str(exc),
        }


def desbloquear_antibot(telefone: str) -> Dict[str, Any]:
    """
    Remove somente o bloqueio ativo e a reincidência.
    Os contadores naturais continuam expirando sozinhos.
    """
    identidade = _normalizar_telefone(telefone)

    try:
        client = _get_redis()
        removidos = client.delete(
            f"{PREFIX}:block:{identidade}",
            f"{PREFIX}:strikes:{identidade}",
        )

        logger.info(
            "Anti-Bot desbloqueado manualmente telefone=%s chaves_removidas=%s",
            identidade,
            removidos,
        )

        return {
            "ok": True,
            "telefone": identidade,
            "chaves_removidas": int(removidos),
        }

    except Exception as exc:
        logger.exception("Erro desbloqueando Anti-Bot: %s", exc)
        return {
            "ok": False,
            "telefone": identidade,
            "erro": str(exc),
        }
