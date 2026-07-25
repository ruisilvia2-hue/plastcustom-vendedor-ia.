"""
Tudo que fala com o banco de dados: o pool de conexões, e as funções que
buscam/criam/atualizam clientes, conversas, mensagens, estado do pedido e leads.
"""
import re

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor, Json

from app.config import logger, DATABASE_URL, SINAIS

_db_pool = None


def get_pool():
    """Cria o pool de conexões só na primeira vez que for realmente necessário
    (não ao importar o arquivo). Isso também é mais seguro com o Gunicorn:
    cada processo worker cria o seu próprio pool depois de nascer."""
    global _db_pool
    if _db_pool is None:
        _db_pool = pg_pool.ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)
    return _db_pool


def get_db():
    return get_pool().getconn()


def release_db(db):
    get_pool().putconn(db)


def limpar_telefone(telefone):
    return re.sub(r'[^0-9]', '', telefone)[:20]


def buscar_ou_criar_cliente(telefone):
    telefone = limpar_telefone(telefone)
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        # Tenta o jeito seguro contra corrida: só funciona se existir uma restrição
        # única (UNIQUE) na coluna telefone. Se duas mensagens chegarem ao mesmo tempo
        # do mesmo número, o banco garante que só um registro é criado.
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
        # Não existe restrição única na tabela ainda -> volta pro comportamento antigo
        # (funciona, mas sem a proteção total contra corrida).
        db.rollback()
        cur.execute("SELECT * FROM clientes WHERE telefone=%s", (telefone,))
        c = cur.fetchone()
        if not c:
            cur.execute("INSERT INTO clientes (telefone) VALUES (%s) RETURNING *", (telefone,))
            c = cur.fetchone()
            db.commit()
    cur.close(); release_db(db)
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
    cur.close(); release_db(db)
    return dict(c)


def obter_estado_pedido(conversa_id):
    """Lê a memória estruturada do pedido (produto/tamanho/material/etc, podendo ter
    vários itens) salva no banco. Se a coluna ainda não existir, volta um estado vazio
    em vez de quebrar - o robô continua funcionando, só sem a memória persistente."""
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


def salvar_estado_pedido(conversa_id, estado):
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


def marcar_conversa_fechada(conversa_id):
    """Depois que um pedido fecha, a conversa não fica mais 'ativa' - assim, se o mesmo
    cliente mandar mensagem de novo no futuro (um pedido novo), ele começa com uma
    memória de pedido limpa, em vez de arrastar o pedido antigo já fechado."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE conversas SET status='fechada' WHERE id=%s", (conversa_id,))
        db.commit()
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível marcar conversa como fechada: {e}")
    finally:
        cur.close(); release_db(db)


def verificar_mensagem_duplicada(mensagem_id, conversa_id):
    """Protege contra reprocessar a MESMA mensagem duas vezes (webhook duplicado - o
    provedor de WhatsApp reenvia o aviso por segurança se a primeira resposta demorou).
    Se mensagem_id não vier preenchido, não faz nada (funciona como antes)."""
    if not mensagem_id:
        return None
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("INSERT INTO mensagens_wa_processadas (id, conversa_id) VALUES (%s, %s)", (mensagem_id, conversa_id))
        db.commit()
        return None  # é novidade, segue o processamento normal
    except psycopg2.Error:
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
    finally:
        cur.close(); release_db(db)


def salvar_mensagem(conversa_id, remetente, conteudo):
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO mensagens (conversa_id, remetente, conteudo) VALUES (%s,%s,%s)", (conversa_id, remetente, conteudo))
    cur.execute("UPDATE conversas SET ultima_mensagem=NOW() WHERE id=%s", (conversa_id,))
    db.commit(); cur.close(); release_db(db)


def obter_historico(conversa_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT remetente, conteudo FROM mensagens WHERE conversa_id=%s ORDER BY timestamp DESC LIMIT 30", (conversa_id,))
    msgs = list(reversed(cur.fetchall()))
    cur.close(); release_db(db)
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
    db.commit(); cur.close(); release_db(db)
    return {"score": score, "categoria": categoria}


def limpar_dados_antigos(meses=12, modo_teste=True):
    """Remove (ou só relata, se modo_teste=True) dados de clientes cuja conversa está
    inativa há mais de `meses` meses E que NUNCA resultou em pedido fechado. Pedidos
    fechados são preservados (motivo de negócio: histórico de vendas)."""
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
        return {"modo": "teste", "conversas_que_seriam_removidas": len(alvos),
                "telefones": [a["telefone"] for a in alvos]}

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
    logger.info(f"Limpeza de dados antigos (LGPD): {removidos} clientes/conversas removidos (inativos há mais de {meses} meses, sem pedido fechado)")
    return {"modo": "executado", "conversas_removidas": removidos}
