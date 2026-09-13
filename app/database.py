"""
Tudo que fala com o banco de dados: o pool de conexões, e as funções que
buscam/criam/atualizam clientes, conversas, mensagens, estado do pedido e leads.

Esta versão também prepara a base do funil comercial autônomo da Plastcustom:
- etapa do funil
- próxima ação/follow-up
- tentativas de follow-up
- pausa real do bot para atendimento humano
- motivo de perda
- última ação comercial

A estrutura nova é criada de forma idempotente na primeira utilização, sem
remover nem alterar as funções já existentes do agente.
"""
import re
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.errors
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor, Json

from app.config import logger, DATABASE_URL, SINAIS

_db_pool: Optional[pg_pool.ThreadedConnectionPool] = None
_estrutura_comercial_pronta = False

ETAPAS_FUNIL = (
    "novo",
    "qualificacao",
    "orcamento",
    "negociacao",
    "fechamento",
    "ganho",
    "perdido",
)


# ============================================================
# CONEXÃO
# ============================================================

def get_pool() -> pg_pool.ThreadedConnectionPool:
    global _db_pool
    if _db_pool is None:
        _db_pool = pg_pool.ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)
    return _db_pool


def get_db() -> Any:
    return get_pool().getconn()


def release_db(db: Any) -> None:
    get_pool().putconn(db)


def verificar_conexao_db() -> bool:
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close(); release_db(db)
        return True
    except Exception as e:
        logger.error(
            "Health check: banco de dados não respondeu",
            extra={"evento": "health_db_falhou", "erro": str(e)},
        )
        return False


# ============================================================
# ESTRUTURA COMERCIAL AUTÔNOMA
# ============================================================

def garantir_estrutura_comercial() -> bool:
    """Cria, uma única vez por processo, os campos usados pelo funil autônomo.

    É seguro rodar em uma base que já tenha alguns desses campos porque todos os
    ADD COLUMN / CREATE INDEX usam IF NOT EXISTS. Se o usuário do banco não tiver
    permissão de ALTER TABLE, o agente continua funcionando normalmente e apenas
    registra o problema no log; as funções comerciais novas retornam modo seguro.
    """
    global _estrutura_comercial_pronta

    if _estrutura_comercial_pronta:
        return True

    db = get_db()
    cur = db.cursor()
    try:
        # Evita que vários workers do Gunicorn tentem migrar exatamente ao mesmo tempo.
        cur.execute("SELECT pg_advisory_xact_lock(734517001)")

        cur.execute("""
            ALTER TABLE conversas
                ADD COLUMN IF NOT EXISTS lead_score INTEGER NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS etapa_funil VARCHAR(30) NOT NULL DEFAULT 'novo',
                ADD COLUMN IF NOT EXISTS proximo_followup_em TIMESTAMPTZ NULL,
                ADD COLUMN IF NOT EXISTS followup_tentativas INTEGER NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS followup_ultimo_em TIMESTAMPTZ NULL,
                ADD COLUMN IF NOT EXISTS bot_pausado BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS motivo_perda TEXT NULL,
                ADD COLUMN IF NOT EXISTS ultima_acao_comercial VARCHAR(120) NULL,
                ADD COLUMN IF NOT EXISTS etapa_atualizada_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversas_funil_status
            ON conversas (etapa_funil, status)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversas_followup_pendente
            ON conversas (proximo_followup_em)
            WHERE status = 'ativa'
              AND bot_pausado = FALSE
              AND proximo_followup_em IS NOT NULL
        """)

        db.commit()
        _estrutura_comercial_pronta = True
        logger.info(
            "Estrutura comercial autônoma pronta",
            extra={"evento": "estrutura_comercial_pronta"},
        )
        return True
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(
            f"Não foi possível preparar estrutura comercial autônoma: {e}",
            extra={"evento": "estrutura_comercial_indisponivel"},
        )
        return False
    finally:
        cur.close(); release_db(db)


def estado_comercial_padrao() -> Dict[str, Any]:
    return {
        "etapa_funil": "novo",
        "lead_score": 0,
        "proximo_followup_em": None,
        "followup_tentativas": 0,
        "followup_ultimo_em": None,
        "bot_pausado": False,
        "motivo_perda": None,
        "ultima_acao_comercial": None,
        "etapa_atualizada_em": None,
        "status": "ativa",
    }


def obter_estado_comercial(conversa_id: str) -> Dict[str, Any]:
    if not garantir_estrutura_comercial():
        return estado_comercial_padrao()

    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT
                etapa_funil,
                lead_score,
                proximo_followup_em,
                followup_tentativas,
                followup_ultimo_em,
                bot_pausado,
                motivo_perda,
                ultima_acao_comercial,
                etapa_atualizada_em,
                status
            FROM conversas
            WHERE id=%s
        """, (conversa_id,))
        row = cur.fetchone()
        return dict(row) if row else estado_comercial_padrao()
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível ler estado comercial: {e}")
        return estado_comercial_padrao()
    finally:
        cur.close(); release_db(db)


def atualizar_etapa_funil(
    conversa_id: str,
    etapa: str,
    ultima_acao: Optional[str] = None,
    motivo_perda: Optional[str] = None,
) -> bool:
    if etapa not in ETAPAS_FUNIL:
        logger.warning(f"Etapa de funil inválida ignorada: {etapa}")
        return False
    if not garantir_estrutura_comercial():
        return False

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE conversas
            SET etapa_funil=%s,
                etapa_atualizada_em=NOW(),
                ultima_acao_comercial=COALESCE(%s, ultima_acao_comercial),
                motivo_perda=CASE WHEN %s='perdido' THEN %s ELSE NULL END,
                proximo_followup_em=CASE
                    WHEN %s IN ('ganho', 'perdido') THEN NULL
                    ELSE proximo_followup_em
                END
            WHERE id=%s
        """, (etapa, ultima_acao, etapa, motivo_perda, etapa, conversa_id))
        db.commit()
        return cur.rowcount > 0
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível atualizar etapa do funil: {e}")
        return False
    finally:
        cur.close(); release_db(db)


def registrar_ultima_acao_comercial(conversa_id: str, acao: str) -> bool:
    if not garantir_estrutura_comercial():
        return False

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE conversas SET ultima_acao_comercial=%s WHERE id=%s",
            (acao[:120], conversa_id),
        )
        db.commit()
        return cur.rowcount > 0
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível registrar última ação comercial: {e}")
        return False
    finally:
        cur.close(); release_db(db)


def agendar_followup(conversa_id: str, quando: Any, acao: str = "followup_agendado") -> bool:
    if not garantir_estrutura_comercial():
        return False

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE conversas
            SET proximo_followup_em=%s,
                ultima_acao_comercial=%s
            WHERE id=%s
              AND status='ativa'
              AND bot_pausado=FALSE
              AND etapa_funil NOT IN ('ganho', 'perdido')
        """, (quando, acao[:120], conversa_id))
        db.commit()
        return cur.rowcount > 0
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível agendar follow-up: {e}")
        return False
    finally:
        cur.close(); release_db(db)


def cancelar_followup(conversa_id: str, acao: str = "followup_cancelado") -> bool:
    if not garantir_estrutura_comercial():
        return False

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE conversas
            SET proximo_followup_em=NULL,
                ultima_acao_comercial=%s
            WHERE id=%s
        """, (acao[:120], conversa_id))
        db.commit()
        return cur.rowcount > 0
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível cancelar follow-up: {e}")
        return False
    finally:
        cur.close(); release_db(db)


def registrar_followup_enviado(conversa_id: str, proximo_followup_em: Any = None) -> bool:
    if not garantir_estrutura_comercial():
        return False

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE conversas
            SET followup_tentativas=followup_tentativas + 1,
                followup_ultimo_em=NOW(),
                proximo_followup_em=%s,
                ultima_acao_comercial='followup_enviado'
            WHERE id=%s
        """, (proximo_followup_em, conversa_id))
        db.commit()
        return cur.rowcount > 0
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível registrar follow-up enviado: {e}")
        return False
    finally:
        cur.close(); release_db(db)


def buscar_followups_vencidos(limite: int = 50) -> List[Dict[str, Any]]:
    """Retorna apenas follow-ups realmente seguros para execução.

    Além de estar vencido, o lead precisa continuar ativo, não pode estar em
    atendimento humano, não pode estar ganho/perdido e a última mensagem precisa
    continuar sendo da IA. Essa última checagem reduz o risco de mandar follow-up
    para quem acabou de responder.
    """
    if not garantir_estrutura_comercial():
        return []

    limite = max(1, min(int(limite), 200))
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT
                c.id AS conversa_id,
                c.cliente_id,
                cl.telefone,
                cl.nome,
                c.etapa_funil,
                c.lead_score,
                c.proximo_followup_em,
                c.followup_tentativas,
                c.ultima_acao_comercial
            FROM conversas c
            JOIN clientes cl ON cl.id = c.cliente_id
            WHERE c.status='ativa'
              AND c.bot_pausado=FALSE
              AND c.etapa_funil NOT IN ('ganho', 'perdido')
              AND c.proximo_followup_em IS NOT NULL
              AND c.proximo_followup_em <= NOW()
              AND (
                    SELECT m.remetente
                    FROM mensagens m
                    WHERE m.conversa_id=c.id
                    ORDER BY m.timestamp DESC
                    LIMIT 1
                  ) = 'ia'
            ORDER BY c.proximo_followup_em ASC
            LIMIT %s
        """, (limite,))
        return [dict(r) for r in cur.fetchall()]
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível buscar follow-ups vencidos: {e}")
        return []
    finally:
        cur.close(); release_db(db)


def pausar_bot(conversa_id: str, motivo: Optional[str] = None) -> bool:
    """Pausa o bot e cancela follow-up pendente para permitir atendimento humano."""
    if not garantir_estrutura_comercial():
        return False

    acao = "bot_pausado"
    if motivo:
        acao = f"bot_pausado: {motivo}"[:120]

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE conversas
            SET bot_pausado=TRUE,
                proximo_followup_em=NULL,
                ultima_acao_comercial=%s
            WHERE id=%s
        """, (acao, conversa_id))
        db.commit()
        return cur.rowcount > 0
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível pausar o bot: {e}")
        return False
    finally:
        cur.close(); release_db(db)


def retomar_bot(conversa_id: str) -> bool:
    if not garantir_estrutura_comercial():
        return False

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE conversas
            SET bot_pausado=FALSE,
                ultima_acao_comercial='bot_retomado'
            WHERE id=%s
        """, (conversa_id,))
        db.commit()
        return cur.rowcount > 0
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível retomar o bot: {e}")
        return False
    finally:
        cur.close(); release_db(db)


def bot_esta_pausado(conversa_id: str) -> bool:
    if not garantir_estrutura_comercial():
        return False

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT bot_pausado FROM conversas WHERE id=%s", (conversa_id,))
        row = cur.fetchone()
        return bool(row and row[0])
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível verificar pausa do bot: {e}")
        return False
    finally:
        cur.close(); release_db(db)


# ============================================================
# CLIENTES / CONVERSAS / PEDIDO
# ============================================================

def limpar_telefone(telefone: str) -> str:
    return re.sub(r'[^0-9]', '', telefone)[:20]


def buscar_ou_criar_cliente(telefone: str) -> Dict[str, Any]:
    telefone = limpar_telefone(telefone)
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "INSERT INTO clientes (telefone) VALUES (%s) ON CONFLICT (telefone) DO NOTHING RETURNING *",
            (telefone,)
        )
        c = cur.fetchone()
        db.commit()
        if not c:
            cur.execute("SELECT * FROM clientes WHERE telefone=%s", (telefone,))
            c = cur.fetchone()
    except psycopg2.Error:
        db.rollback()
        cur.execute("SELECT * FROM clientes WHERE telefone=%s", (telefone,))
        c = cur.fetchone()
        if not c:
            cur.execute("INSERT INTO clientes (telefone) VALUES (%s) RETURNING *", (telefone,))
            c = cur.fetchone()
            db.commit()
    cur.close(); release_db(db)
    return dict(c)


def buscar_ou_criar_conversa(cliente_id: str) -> Dict[str, Any]:
    # Prepara a base comercial sem impedir o atendimento caso a migração falhe.
    garantir_estrutura_comercial()

    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT * FROM conversas
        WHERE cliente_id=%s
          AND (status='ativa' OR ultima_mensagem > NOW() - INTERVAL '30 minutes')
        ORDER BY ultima_mensagem DESC LIMIT 1
    """, (cliente_id,))
    c = cur.fetchone()
    if not c:
        cur.execute("INSERT INTO conversas (cliente_id) VALUES (%s) RETURNING *", (cliente_id,))
        c = cur.fetchone()
        db.commit()
    cur.close(); release_db(db)
    return dict(c)


def obter_estado_pedido(conversa_id: str) -> Dict[str, Any]:
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT estado_pedido FROM conversas WHERE id=%s", (conversa_id,))
        row = cur.fetchone()
        estado = row["estado_pedido"] if row else None
        return estado if estado else {"itens": [], "observacoes": ""}
    except psycopg2.Error:
        db.rollback()
        return {"itens": [], "observacoes": ""}
    finally:
        cur.close(); release_db(db)


def salvar_estado_pedido(conversa_id: str, estado: Dict[str, Any]) -> None:
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE conversas SET estado_pedido=%s WHERE id=%s", (Json(estado), conversa_id))
        db.commit()
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível salvar estado_pedido (a coluna pode não existir ainda no banco): {e}")
    finally:
        cur.close(); release_db(db)


def marcar_conversa_fechada(conversa_id: str) -> None:
    estrutura_ok = garantir_estrutura_comercial()
    db = get_db()
    cur = db.cursor()
    try:
        if estrutura_ok:
            cur.execute("""
                UPDATE conversas
                SET status='fechada',
                    etapa_funil='ganho',
                    etapa_atualizada_em=NOW(),
                    proximo_followup_em=NULL,
                    ultima_acao_comercial='pedido_fechado'
                WHERE id=%s
            """, (conversa_id,))
        else:
            cur.execute("UPDATE conversas SET status='fechada' WHERE id=%s", (conversa_id,))
        db.commit()
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível marcar conversa como fechada: {e}")
    finally:
        cur.close(); release_db(db)


# ============================================================
# IDEMPOTÊNCIA / MENSAGENS
# ============================================================

def verificar_mensagem_duplicada(mensagem_id: Optional[str], conversa_id: str) -> Optional[str]:
    if not mensagem_id:
        return None
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("INSERT INTO mensagens_wa_processadas (id, conversa_id) VALUES (%s, %s)", (mensagem_id, conversa_id))
        db.commit()
        return None
    except psycopg2.errors.UniqueViolation:
        db.rollback()
        try:
            cur.execute(
                "SELECT conteudo FROM mensagens WHERE conversa_id=%s AND remetente='ia' ORDER BY timestamp DESC LIMIT 1",
                (conversa_id,)
            )
            row = cur.fetchone()
            return row["conteudo"] if row else "Só um segundo, já te respondo! 😊"
        except psycopg2.Error:
            return "Só um segundo, já te respondo! 😊"
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(
            f"Proteção contra mensagem duplicada indisponível (tabela pode não existir ainda): {e}",
            extra={"evento": "dedup_indisponivel"},
        )
        return None
    finally:
        cur.close(); release_db(db)


def salvar_mensagem(conversa_id: str, remetente: str, conteudo: str) -> Any:
    estrutura_ok = garantir_estrutura_comercial()
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "INSERT INTO mensagens (conversa_id, remetente, conteudo) VALUES (%s,%s,%s) RETURNING timestamp",
        (conversa_id, remetente, conteudo)
    )
    timestamp = cur.fetchone()["timestamp"]

    # Uma resposta nova do cliente invalida qualquer follow-up que estava agendado.
    if remetente == "cliente" and estrutura_ok:
        cur.execute("""
            UPDATE conversas
            SET ultima_mensagem=NOW(),
                proximo_followup_em=NULL,
                ultima_acao_comercial='cliente_respondeu'
            WHERE id=%s
        """, (conversa_id,))
    else:
        cur.execute("UPDATE conversas SET ultima_mensagem=NOW() WHERE id=%s", (conversa_id,))

    db.commit(); cur.close(); release_db(db)
    return timestamp


def existe_mensagem_cliente_mais_nova(conversa_id: str, apos: Any) -> bool:
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT 1 FROM mensagens WHERE conversa_id=%s AND remetente='cliente' AND timestamp > %s LIMIT 1",
        (conversa_id, apos)
    )
    existe = cur.fetchone() is not None
    cur.close(); release_db(db)
    return existe


def obter_historico(conversa_id: str) -> List[Dict[str, Any]]:
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT remetente, conteudo FROM mensagens WHERE conversa_id=%s ORDER BY timestamp DESC LIMIT 20", (conversa_id,))
    msgs = list(reversed(cur.fetchall()))
    cur.close(); release_db(db)
    return msgs


# ============================================================
# REENGAJAMENTO LEGADO — mantido para não quebrar o fluxo atual
# ============================================================

def buscar_conversas_para_reengajar(horas_min: float = 3, horas_max: float = 24) -> List[Dict[str, Any]]:
    """Encontra conversas 'esfriando': o robô foi quem falou por último, o cliente não
    respondeu desde então, já passou entre horas_min e horas_max horas de silêncio, a
    conversa ainda está ativa (não fechada/concluída), e ELA AINDA NÃO recebeu nenhum
    reengajamento antes (ver notificacoes.tipo='reengajamento' - garante no máximo UMA
    mensagem de retomada por conversa, nunca insiste/spamma o cliente)."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT c.id AS conversa_id, c.cliente_id, cl.telefone, cl.nome
        FROM conversas c
        JOIN clientes cl ON cl.id = c.cliente_id
        WHERE c.status = 'ativa'
          AND c.ultima_mensagem BETWEEN NOW() - (%s || ' hours')::INTERVAL
                                     AND NOW() - (%s || ' hours')::INTERVAL
          AND (
                SELECT m.remetente FROM mensagens m
                WHERE m.conversa_id = c.id
                ORDER BY m.timestamp DESC LIMIT 1
              ) = 'ia'
          AND NOT EXISTS (
                SELECT 1 FROM notificacoes n
                WHERE n.conversa_id = c.id AND n.tipo = 'reengajamento'
              )
    """, (horas_max, horas_min))
    resultado = [dict(r) for r in cur.fetchall()]
    cur.close(); release_db(db)
    return resultado


# ============================================================
# LEAD SCORE
# ============================================================

def calcular_score(conversa_id: str, cliente_id: str) -> Dict[str, Any]:
    # lead_score já era usado pelo sistema atual; a estrutura comercial garante a coluna.
    garantir_estrutura_comercial()

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
    cur.execute(
        "INSERT INTO leads (cliente_id, conversa_id, score, categoria) VALUES (%s,%s,%s,%s)",
        (cliente_id, conversa_id, score, categoria),
    )
    cur.execute("UPDATE conversas SET lead_score=%s WHERE id=%s", (score, conversa_id))
    db.commit(); cur.close(); release_db(db)
    return {"score": score, "categoria": categoria}


# ============================================================
# LGPD / LIMPEZA
# ============================================================

def limpar_dados_antigos(meses: int = 12, modo_teste: bool = True) -> Dict[str, Any]:
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT c.id AS conversa_id, c.cliente_id, cl.telefone
        FROM conversas c
        JOIN clientes cl ON cl.id = c.cliente_id
        WHERE c.ultima_mensagem < NOW() - (%s || ' months')::INTERVAL
          AND NOT EXISTS (
              SELECT 1 FROM notificacoes n
              WHERE n.conversa_id = c.id AND n.tipo = 'pedido_fechado'
          )
    """, (meses,))
    alvos = cur.fetchall()

    if modo_teste:
        cur.close(); release_db(db)
        return {
            "modo": "teste",
            "conversas_que_seriam_removidas": len(alvos),
            "telefones": [a["telefone"] for a in alvos],
        }

    removidos = 0
    for alvo in alvos:
        cur.execute("DELETE FROM mensagens WHERE conversa_id=%s", (alvo["conversa_id"],))
        cur.execute("DELETE FROM leads WHERE conversa_id=%s", (alvo["conversa_id"],))
        cur.execute("DELETE FROM notificacoes WHERE conversa_id=%s", (alvo["conversa_id"],))
        cur.execute("DELETE FROM conversas WHERE id=%s", (alvo["conversa_id"],))
        cur.execute("DELETE FROM clientes WHERE id=%s", (alvo["cliente_id"],))
        removidos += 1
    db.commit()
    cur.close(); release_db(db)
    logger.info(
        f"Limpeza de dados antigos (LGPD): {removidos} clientes/conversas removidos "
        f"(inativos há mais de {meses} meses, sem pedido fechado)"
    )
    return {"modo": "executado", "conversas_removidas": removidos}
