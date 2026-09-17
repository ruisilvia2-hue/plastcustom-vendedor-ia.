"""Camada de Conversion Rate Optimization do Vendedor IA.

O CRO observa o funil e recomenda experimentos; não altera preços, regras de
produção ou o prompt ativo automaticamente. Toda mudança começa como draft.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.config import logger
from app.database import get_db, release_db, garantir_estrutura_comercial

EVENTOS = {
    "mensagem_cliente", "resposta_ia", "orcamento_apresentado", "pedido_fechado",
    "transferencia_humana", "followup_enviado", "abandono_estimado",
}
VARIANTES = {"controle", "variante_a", "variante_b"}


def _safe_json(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def registrar_evento(conversa_id: str, cliente_id: Optional[str], evento: str,
                     metadata: Optional[Dict[str, Any]] = None,
                     experimento_id: Optional[str] = None,
                     variante: Optional[str] = None) -> bool:
    if evento not in EVENTOS:
        return False
    if variante is not None and variante not in VARIANTES:
        return False
    garantir_estrutura_comercial()
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO cro_eventos
              (conversa_id, cliente_id, evento, metadata, experimento_id, variante)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (conversa_id, cliente_id, evento, json.dumps(_safe_json(metadata), ensure_ascii=False),
              experimento_id, variante))
        db.commit(); return True
    except Exception as exc:
        db.rollback(); logger.warning("Evento CRO não registrado", extra={"evento": "cro_evento_falhou", "erro": str(exc)})
        return False
    finally:
        cur.close(); release_db(db)


def _bucket(conversa_id: str, experimento_id: str) -> float:
    raw = hashlib.sha256(f"{experimento_id}:{conversa_id}".encode()).hexdigest()[:8]
    return int(raw, 16) / 0xFFFFFFFF


def atribuir_experimento(conversa_id: str, experimento_id: str) -> Optional[str]:
    """Atribui uma variante de forma estável; nunca ativa experimento sozinho."""
    garantir_estrutura_comercial()
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT percentual_variante, status FROM cro_experimentos WHERE id=%s", (experimento_id,))
        row = cur.fetchone()
        if not row or row[1] != "active": return None
        variante = "variante_a" if _bucket(conversa_id, experimento_id) < float(row[0]) / 100 else "controle"
        cur.execute("""INSERT INTO cro_alocacoes (experimento_id, conversa_id, variante)
                       VALUES (%s,%s,%s) ON CONFLICT (experimento_id, conversa_id)
                       DO UPDATE SET variante=EXCLUDED.variante RETURNING variante""", (experimento_id, conversa_id, variante))
        result = cur.fetchone()[0]; db.commit(); return result
    except Exception as exc:
        db.rollback(); logger.warning("Alocação CRO indisponível", extra={"evento": "cro_alocacao_falhou", "erro": str(exc)}); return None
    finally:
        cur.close(); release_db(db)


def criar_experimento(nome: str, hipotese: str, metrica: str = "pedido_fechado",
                      percentual_variante: float = 50) -> Dict[str, Any]:
    if not nome or not hipotese or metrica not in {"pedido_fechado", "orcamento_apresentado", "transferencia_humana"}:
        return {"ok": False, "erro": "dados_invalidos"}
    if not 5 <= float(percentual_variante) <= 95:
        return {"ok": False, "erro": "percentual_fora_do_limite"}
    garantir_estrutura_comercial(); db = get_db(); cur = db.cursor()
    try:
        cur.execute("""INSERT INTO cro_experimentos
          (nome, hipotese, metrica, percentual_variante, status)
          VALUES (%s,%s,%s,%s,'draft') RETURNING id""", (nome[:160], hipotese[:1000], metrica, percentual_variante))
        eid = cur.fetchone()[0]; db.commit(); return {"ok": True, "id": str(eid), "status": "draft"}
    except Exception as exc:
        db.rollback(); logger.warning("Experimento CRO não criado", extra={"evento": "cro_experimento_falhou", "erro": str(exc)}); return {"ok": False, "erro": "falha_banco"}
    finally:
        cur.close(); release_db(db)


def relatorio(horas: int = 720) -> Dict[str, Any]:
    """Relatório CRO com conversão calculada por conversa única.

    As contagens brutas de eventos continuam disponíveis em ``eventos`` para
    diagnóstico de volume. As taxas do funil usam ``COUNT(DISTINCT conversa_id)``
    para impedir que várias mensagens ou revisões de orçamento da mesma conversa
    inflem ou reduzam artificialmente a conversão.
    """
    horas = max(1, min(int(horas), 8760))
    garantir_estrutura_comercial()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""SELECT evento, COUNT(*) FROM cro_eventos
                       WHERE criado_em >= NOW() - (%s || ' hours')::interval
                       GROUP BY evento ORDER BY evento""", (horas,))
        contagens = {r[0]: r[1] for r in cur.fetchall()}

        cur.execute("""SELECT evento, COUNT(DISTINCT conversa_id) FROM cro_eventos
                       WHERE criado_em >= NOW() - (%s || ' hours')::interval
                       GROUP BY evento ORDER BY evento""", (horas,))
        conversas = {r[0]: r[1] for r in cur.fetchall()}

        def rate(a: str, b: str) -> float:
            denominador = conversas.get(b, 0)
            if denominador <= 0:
                return 0.0
            return round(conversas.get(a, 0) / denominador * 100, 2)

        taxas = {
            "mensagem_para_orcamento": rate("orcamento_apresentado", "mensagem_cliente"),
            "orcamento_para_fechamento": rate("pedido_fechado", "orcamento_apresentado"),
            "transferencia": rate("transferencia_humana", "mensagem_cliente"),
        }
        return {
            "ok": True,
            "janela_horas": horas,
            "eventos": contagens,
            "conversas_unicas": conversas,
            "taxas": taxas,
            "hipoteses": gerar_hipoteses(conversas),
        }
    finally:
        cur.close()
        release_db(db)


def gerar_hipoteses(c: Dict[str, int]) -> list[Dict[str, Any]]:
    """Gera hipóteses usando volumes de conversas únicas por etapa do funil."""
    out = []
    mensagens = c.get("mensagem_cliente", 0)
    orcamentos = c.get("orcamento_apresentado", 0)
    fechamentos = c.get("pedido_fechado", 0)
    transferencias = c.get("transferencia_humana", 0)

    if mensagens >= 20 and orcamentos / max(mensagens, 1) < .25:
        out.append({"prioridade": "alta", "hipotese": "Reduzir perguntas antes do primeiro orçamento pode aumentar avanço no funil.", "metrica": "orcamento_apresentado"})
    if orcamentos >= 10 and fechamentos / max(orcamentos, 1) < .10:
        out.append({"prioridade": "alta", "hipotese": "Testar apresentação de preço por milheiro e total pode melhorar o fechamento.", "metrica": "pedido_fechado"})
    if mensagens > 0 and transferencias / mensagens > .30:
        out.append({"prioridade": "media", "hipotese": "Mapear os motivos de transferência pode revelar lacunas de conhecimento do vendedor.", "metrica": "transferencia_humana"})
    return out
