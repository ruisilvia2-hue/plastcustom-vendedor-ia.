"""
As rotas HTTP do robô: /webhook (recebe mensagens do n8n), rotas administrativas
e /health.

O /webhook aceita texto e também imagens recebidas pelo WhatsApp. As imagens não
são gravadas em base64 no banco: são baixadas da Evolution apenas durante o
processamento e enviadas à IA para análise visual.
"""
import json
import time
from typing import Any, Dict, List

from flask import Blueprint, request, jsonify

from app.config import logger, WEBHOOK_SECRET, limiter, correlation_id_var
from app.database import (
    buscar_ou_criar_cliente, buscar_ou_criar_conversa, verificar_mensagem_duplicada,
    salvar_mensagem, obter_historico, obter_estado_pedido, calcular_score,
    limpar_dados_antigos, existe_mensagem_cliente_mais_nova, verificar_conexao_db,
    buscar_conversas_para_reengajar, bot_esta_pausado, retomar_bot_por_telefone,
)
from app.whatsapp import (
    notificar_proprietario,
    reengajar_cliente,
    obter_midia_base64,
)
from app.ia import gerar_resposta, SYSTEM_PROMPT
from app.precos import recarregar_tabela_precos
from app.antibot import verificar_antibot
from app.cro import registrar_evento

bp = Blueprint("webhook", __name__)

PALAVRAS_RECUSA_ORCAMENTO = [
    "não", "nao", "deixa", "desisto", "sem interesse", "outra hora", "obrigad"
]

CHAVES_MIDIA = {
    "imageMessage",
    "audioMessage",
    "videoMessage",
    "documentMessage",
    "stickerMessage",
}


def _normalizar_raw_messages(valor: Any) -> List[Dict[str, Any]]:
    """Aceita o array nativo do n8n ou, por segurança, uma string JSON."""
    if valor is None:
        return []

    if isinstance(valor, str):
        valor = valor.strip()
        if not valor:
            return []
        try:
            valor = json.loads(valor)
        except (TypeError, ValueError):
            logger.warning(
                "raw_messages chegou como string inválida",
                extra={"evento": "raw_messages_invalido"},
            )
            return []

    if isinstance(valor, dict):
        valor = [valor]

    if not isinstance(valor, list):
        return []

    return [item for item in valor if isinstance(item, dict)]


def _contem_chave_recursiva(valor: Any, chaves: set, profundidade: int = 0) -> bool:
    """Localiza imageMessage etc. inclusive dentro de wrappers view-once."""
    if profundidade > 7:
        return False

    if isinstance(valor, dict):
        if any(chave in valor for chave in chaves):
            return True
        return any(
            _contem_chave_recursiva(v, chaves, profundidade + 1)
            for v in valor.values()
        )

    if isinstance(valor, list):
        return any(
            _contem_chave_recursiva(v, chaves, profundidade + 1)
            for v in valor
        )

    return False


def _raw_eh_imagem(raw: Dict[str, Any]) -> bool:
    tipo = str(raw.get("messageType") or raw.get("type") or "").lower()
    if "image" in tipo:
        return True
    return _contem_chave_recursiva(raw.get("message") or raw, {"imageMessage"})


def _raw_tem_qualquer_midia(raw: Dict[str, Any]) -> bool:
    tipo = str(raw.get("messageType") or raw.get("type") or "").lower()
    if any(x in tipo for x in ("image", "audio", "video", "document", "sticker")):
        return True
    return _contem_chave_recursiva(raw.get("message") or raw, CHAVES_MIDIA)


@bp.route("/webhook", methods=["POST"])
@limiter.limit("30 per minute")
def webhook():
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"erro": "não autorizado"}), 401

    data = request.get_json(silent=True) or {}

    telefone_raw = (data.get("telefone") or "").strip()[:30]
    mensagem = (data.get("mensagem") or "").strip()[:2000]
    instance = (data.get("instance") or "automacao").strip()[:100]
    raw_messages = _normalizar_raw_messages(data.get("raw_messages"))

    if not telefone_raw:
        return jsonify({"erro": "dados incompletos"}), 400

    try:
        cliente = buscar_ou_criar_cliente(telefone_raw)
        conversa = buscar_ou_criar_conversa(cliente["id"])
        correlation_id_var.set(str(conversa["id"])[:8])

        # Deduplicação, quando o n8n enviar mensagem_id.
        mensagem_id = (data.get("mensagem_id") or "")[:100] or None
        resposta_duplicada = verificar_mensagem_duplicada(mensagem_id, conversa["id"])
        if resposta_duplicada is not None:
            # IMPORTANTE: uma duplicata NUNCA deve reenviar ao WhatsApp a resposta
            # anterior. O n8n só envia mensagens quando "resposta" não está vazia.
            # Assim, o primeiro processamento vence e qualquer execução paralela com
            # o mesmo mensagem_id termina silenciosamente antes de chamar a IA.
            logger.warning(
                f"Mensagem duplicada detectada (id={mensagem_id}) - descartando reprocessamento"
            )
            return jsonify({
                "ok": True,
                "resposta": "",
                "duplicado": True,
            }), 200

        # Handoff humano: não baixa mídia, não chama IA e não gasta créditos.
        if bot_esta_pausado(conversa["id"]):
            registro = mensagem or "[cliente enviou mídia durante atendimento humano]"
            salvar_mensagem(conversa["id"], "cliente", registro)
            registrar_evento(
                str(conversa["id"]),
                str(cliente["id"]),
                "mensagem_cliente",
                {"origem": "webhook", "bot_pausado": True},
            )
            logger.info(
                "Mensagem recebida durante atendimento humano - bot permaneceu silencioso",
                extra={"evento": "bot_pausado_silencio", "conversa_id": conversa["id"]},
            )
            return jsonify({
                "ok": True,
                "resposta": "",
                "bot_pausado": True,
            }), 200

        ha_imagem_no_lote = any(_raw_eh_imagem(raw) for raw in raw_messages)
        ha_midia_no_lote = any(_raw_tem_qualquer_midia(raw) for raw in raw_messages)

        # Anti-bot também protege mensagens que contêm somente mídia.
        sinal_antibot = mensagem or ("[imagem]" if ha_imagem_no_lote else "[midia]")
        protecao_antibot = verificar_antibot(telefone_raw, sinal_antibot)

        if not protecao_antibot.get("allow_ai", False):
            logger.warning(
                "Mensagem barrada pelo Anti-Bot",
                extra={
                    "evento": "antibot_bloqueio",
                    "telefone": telefone_raw,
                    "motivo": protecao_antibot.get("motivo"),
                    "retry_after_seconds": protecao_antibot.get("retry_after_seconds", 0),
                    "counters": protecao_antibot.get("counters", {}),
                },
            )
            return jsonify({
                "ok": True,
                "resposta": "",
                "bloqueado_antibot": True,
                "retry_after_seconds": protecao_antibot.get("retry_after_seconds", 0),
            }), 200

        # ================================================================
        # IMAGENS
        # ================================================================
        imagens = []
        captions = []
        falhas_imagem = 0

        for raw in raw_messages:
            if not _raw_eh_imagem(raw):
                continue

            resultado_midia = obter_midia_base64(instance, raw)

            if resultado_midia.get("ok") and resultado_midia.get("imagem_suportada"):
                imagens.append({
                    "base64": resultado_midia["base64"],
                    "mimetype": resultado_midia["mimetype"],
                    "file_name": resultado_midia.get("file_name"),
                })

                caption = (resultado_midia.get("caption") or "").strip()
                if caption and caption not in captions:
                    captions.append(caption)
            else:
                falhas_imagem += 1
                logger.warning(
                    "Imagem recebida, mas não pôde ser preparada para a IA",
                    extra={
                        "evento": "imagem_nao_processada",
                        "conversa_id": conversa["id"],
                        "erro": resultado_midia.get("erro"),
                        "mimetype": resultado_midia.get("mimetype"),
                    },
                )

        # Junta texto digitado + captions das imagens, sem repetir.
        partes_texto = []
        if mensagem:
            partes_texto.append(mensagem)

        for caption in captions:
            if caption and caption not in partes_texto:
                partes_texto.append(caption)

        texto_cliente = "\n".join(partes_texto).strip()

        # Se só chegou mídia ainda não suportada (áudio/documento/figurinha/vídeo),
        # mantém o comportamento seguro de pedir texto.
        if not texto_cliente and not imagens:
            registro = "[cliente enviou uma mídia não suportada para análise]"
            salvar_mensagem(conversa["id"], "cliente", registro)
            registrar_evento(
                str(conversa["id"]),
                str(cliente["id"]),
                "mensagem_cliente",
                {"origem": "webhook", "midia_nao_suportada": True},
            )

            if ha_imagem_no_lote and falhas_imagem:
                resposta = (
                    "Recebi sua imagem, mas tive um problema para abrir ela aqui agora 🙏 "
                    "Se puder, tente reenviar a imagem ou me descreva o que você precisa."
                )
            elif ha_midia_no_lote:
                resposta = (
                    "Recebi sua mídia, mas por enquanto consigo analisar imagens. "
                    "Se for áudio, vídeo, documento ou figurinha, pode me escrever em texto o que você precisa? 🙏"
                )
            else:
                resposta = (
                    "Recebi algo aqui, mas não consegui identificar o conteúdo 🙏 "
                    "Pode me escrever em texto o que você precisa?"
                )

            salvar_mensagem(conversa["id"], "ia", resposta)
            registrar_evento(
                str(conversa["id"]),
                str(cliente["id"]),
                "resposta_ia",
                {"origem": "webhook", "resposta_reserva_midia": True},
            )
            return jsonify({"ok": True, "resposta": resposta}), 200

        if imagens and not texto_cliente:
            texto_cliente = (
                f"O cliente enviou {len(imagens)} imagem(ns) como referência para o pedido. "
                "Analise as imagens antes de responder."
            )

        registro_historico = texto_cliente
        if imagens:
            sufixo = f"[{len(imagens)} imagem(ns) analisável(is) anexada(s)]"
            registro_historico = (
                f"{texto_cliente}\n{sufixo}" if texto_cliente else sufixo
            )

        timestamp_minha_mensagem = salvar_mensagem(
            conversa["id"], "cliente", registro_historico
        )
        registrar_evento(
            str(conversa["id"]),
            str(cliente["id"]),
            "mensagem_cliente",
            {
                "origem": "webhook",
                "tem_imagem": bool(imagens),
                "quantidade_imagens": len(imagens),
            },
        )

        # Mantém a proteção existente contra respostas concorrentes.
        time.sleep(3)
        if existe_mensagem_cliente_mais_nova(
            conversa["id"], timestamp_minha_mensagem
        ):
            logger.info(
                "Mensagem superada por outra mais recente - não respondendo separadamente",
                extra={"evento": "mensagem_superada", "conversa_id": conversa["id"]},
            )
            return jsonify({"ok": True, "resposta": "", "superada": True})

        historico = obter_historico(conversa["id"])

        messages = []
        for m in historico[:-1]:
            role = "user" if m["remetente"] == "cliente" else "assistant"
            messages.append({"role": role, "content": m["conteudo"]})

        messages.append({"role": "user", "content": texto_cliente})

        partes_contexto_extra = []

        if not historico[:-1]:
            partes_contexto_extra.append(
                "CONTEXTO: esta é a PRIMEIRA mensagem desta conversa - inclua a nota curta "
                "de privacidade no final da sua resposta, como instruído acima."
            )

        estado_atual = obter_estado_pedido(conversa["id"])
        if estado_atual.get("itens"):
            partes_contexto_extra.append(
                "ESTADO ATUAL DO PEDIDO (já confirmado nesta conversa - NÃO pergunte "
                "de novo o que já está aqui):\n"
                + json.dumps(estado_atual, ensure_ascii=False)
            )

        if imagens:
            partes_contexto_extra.append(
                f"CONTEXTO VISUAL: o cliente anexou {len(imagens)} imagem(ns) nesta mensagem. "
                "Observe-as antes de perguntar qualquer dado que possa estar claramente visível."
            )

        if (
            len(historico) >= 2
            and historico[-2]["remetente"] == "ia"
            and "Orçamento Plastcustom" in historico[-2]["conteudo"]
        ):
            mensagem_curta = len(texto_cliente) < 60
            parece_recusa = mensagem_curta and any(
                p in texto_cliente.lower() for p in PALAVRAS_RECUSA_ORCAMENTO
            )
            if parece_recusa:
                partes_contexto_extra.append(
                    "CONTEXTO: você acabou de apresentar um orçamento e o cliente respondeu "
                    "recusando de forma direta e curta. NÃO encerre a conversa educadamente "
                    "sem reagir - pergunte o motivo de forma leve (valor, quantidade, prazo?) "
                    "e ofereça pelo menos UMA alternativa concreta antes de aceitar como "
                    "resposta final."
                )

        contexto_extra = "\n\n".join(partes_contexto_extra)

        resposta = gerar_resposta(
            messages,
            contexto_extra,
            cliente,
            conversa,
            imagens=imagens,
        )

        if not resposta:
            logger.warning("IA devolveu resposta vazia - usando frase de reserva")
            resposta = (
                "Desculpa, tive um probleminha aqui rapidinho 🙏 "
                "Pode repetir sua última mensagem?"
            )

        salvar_mensagem(conversa["id"], "ia", resposta)
        registrar_evento(
            str(conversa["id"]),
            str(cliente["id"]),
            "resposta_ia",
            {"origem": "webhook"},
        )
        lead = calcular_score(conversa["id"], cliente["id"])

        if lead["score"] >= 80:
            notificar_proprietario(cliente, lead["score"], conversa["id"])

        return jsonify({
            "ok": True,
            "resposta": resposta,
            "score": lead["score"],
            "categoria": lead["categoria"],
            "imagens_processadas": len(imagens),
        })

    except Exception as e:
        logger.error(f"Erro inesperado no /webhook: {e}")
        return jsonify({
            "ok": False,
            "resposta": (
                "Desculpa, tive um probleminha aqui rapidinho 🙏 "
                "Pode repetir sua última mensagem?"
            ),
        }), 200


@bp.route("/manutencao/limpeza", methods=["POST"])
@limiter.limit("10 per hour")  # endpoint administrativo, uso esperado é raro
def manutencao_limpeza():
    """Endpoint protegido para a limpeza periódica de dados antigos (LGPD).
    Por padrão roda em modo de TESTE (não apaga nada) - só passa a apagar de
    verdade se o corpo da requisição incluir {"modo": "executar"}."""
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"erro": "não autorizado"}), 401
    data = request.get_json(silent=True) or {}
    meses = data.get("meses", 12)
    modo_teste = data.get("modo") != "executar"
    try:
        resultado = limpar_dados_antigos(meses=meses, modo_teste=modo_teste)
        return jsonify(resultado)
    except Exception as e:
        # NÃO devolve str(e) pra fora - detalhe técnico interno (nome de tabela,
        # estrutura do banco) não deveria vazar nem pra quem tem o segredo certo.
        # O erro de verdade fica só no log, pra você/eu debugarmos.
        logger.error(
            "Erro na limpeza de dados antigos",
            extra={"evento": "erro_limpeza_dados", "erro": str(e)},
        )
        return jsonify({"erro": "Não foi possível concluir a limpeza agora. Verifique os logs do serviço."}), 500


@bp.route("/admin/retomar-bot", methods=["POST"])
@limiter.limit("30 per hour")
def admin_retomar_bot():
    """Retoma o atendimento automático de um cliente pelo telefone.

    Endpoint administrativo protegido pelo mesmo X-Webhook-Secret já usado
    nas outras rotas internas. Não envia mensagem ao cliente; apenas libera
    a conversa para que a próxima mensagem volte a ser atendida pela IA.
    """
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"erro": "não autorizado"}), 401

    data = request.get_json(silent=True) or {}
    telefone = (data.get("telefone") or "").strip()[:30]

    if not telefone:
        return jsonify({
            "ok": False,
            "retomado": False,
            "erro": "telefone obrigatório",
        }), 400

    resultado = retomar_bot_por_telefone(telefone)
    status = 200 if resultado.get("ok") else 500
    return jsonify(resultado), status


@bp.route("/admin/recarregar-precos", methods=["POST"])
@limiter.limit("10 per hour")  # endpoint administrativo, uso esperado é raro
def admin_recarregar_precos():
    """Relê o arquivo Plastcustom_Orcamento.html e atualiza os preços em uso,
    sem precisar reiniciar o serviço. Protegido pelo mesmo segredo do /webhook."""
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"erro": "não autorizado"}), 401
    try:
        resultado = recarregar_tabela_precos()
        status = 200 if resultado["sucesso"] else 500
        return jsonify(resultado), status
    except Exception as e:
        logger.error(
            "Erro inesperado ao recarregar preços",
            extra={"evento": "erro_recarregar_precos", "erro": str(e)},
        )
        # Idem: não devolve str(e) pra fora, só um erro genérico + log detalhado internamente.
        return jsonify({"sucesso": False, "erro": "Não foi possível recarregar os preços agora. Verifique os logs do serviço."}), 500


@bp.route("/admin/reengajar-conversas", methods=["POST"])
@limiter.limit("30 per hour")  # chamado periodicamente por um agendador (ex: n8n a cada 30-60min)
def admin_reengajar_conversas():
    """Encontra conversas onde o robô falou por último e o cliente sumiu (silêncio entre
    horas_min e horas_max horas) e manda UMA única mensagem curta de retomada por conversa
    - nunca insiste duas vezes. Pensado pra ser chamado periodicamente por um agendador
    externo (ex: n8n Schedule Trigger a cada 30-60 minutos), não pelo n8n do webhook normal.
    Por padrão roda em modo de TESTE (não manda nada) - só manda de verdade se o corpo da
    requisição incluir {"modo": "executar"}."""
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"erro": "não autorizado"}), 401
    data = request.get_json(silent=True) or {}
    horas_min = data.get("horas_min", 3)
    horas_max = data.get("horas_max", 24)
    modo_teste = data.get("modo") != "executar"
    try:
        alvos = buscar_conversas_para_reengajar(horas_min, horas_max)
        if modo_teste:
            return jsonify({
                "modo": "teste",
                "conversas_que_receberiam_reengajamento": len(alvos),
                "telefones": [a["telefone"] for a in alvos],
            })
        enviados = 0
        for alvo in alvos:
            if reengajar_cliente(alvo["telefone"], alvo.get("nome"), alvo["conversa_id"], alvo["cliente_id"]):
                enviados += 1
        return jsonify({"modo": "executado", "conversas_reengajadas": enviados, "total_candidatas": len(alvos)})
    except Exception as e:
        logger.error(
            "Erro ao reengajar conversas",
            extra={"evento": "erro_reengajamento", "erro": str(e)},
        )
        return jsonify({"erro": "Não foi possível concluir o reengajamento agora. Verifique os logs do serviço."}), 500


@bp.route("/health", methods=["GET"])
def health():
    """Checa se o serviço está de pé E se o banco está respondendo. Pensado pra
    ser usado por um monitor externo gratuito (ex: UptimeRobot) que avisa você
    por e-mail/SMS se isso ficar fora do ar - não precisa de Datadog/Prometheus
    pra um único serviço como este."""
    banco_ok = verificar_conexao_db()
    status_geral = "ok" if banco_ok else "degradado"
    codigo_http = 200 if banco_ok else 503
    return jsonify({"status": status_geral, "banco_de_dados": "ok" if banco_ok else "falhou"}), codigo_http
