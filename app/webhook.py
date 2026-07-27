"""
As rotas HTTP do robô: /webhook (recebe mensagens do n8n), /manutencao/limpeza
(limpeza periódica de dados antigos, LGPD) e /health (checagem de saúde do serviço).

Este módulo é propositalmente enxuto: ele só orquestra a chamada dos outros módulos
(database, whatsapp, ia) - a lógica de negócio de verdade vive lá, não aqui.
"""
import json
import time

from flask import Blueprint, request, jsonify

from app.config import logger, WEBHOOK_SECRET, limiter
from app.database import (
    buscar_ou_criar_cliente, buscar_ou_criar_conversa, verificar_mensagem_duplicada,
    salvar_mensagem, obter_historico, obter_estado_pedido, calcular_score,
    limpar_dados_antigos, existe_mensagem_cliente_mais_nova,
)
from app.whatsapp import notificar_proprietario
from app.ia import gerar_resposta, SYSTEM_PROMPT
from app.precos import recarregar_tabela_precos

bp = Blueprint("webhook", __name__)


@bp.route("/webhook", methods=["POST"])
@limiter.limit("30 per minute")  # bem acima do que um uso normal precisa, mas trava abuso/DoS
def webhook():
    # Autenticação: só aceita chamadas que tragam o segredo combinado com o n8n.
    # Sem isso, qualquer pessoa na internet que descobrisse esse endereço poderia
    # gastar seus créditos de IA e mandar mensagens em nome do robô.
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"erro": "não autorizado"}), 401

    # silent=True: se o corpo vier malformado (não é JSON válido), devolve None em vez
    # de levantar uma exceção não tratada - "or {}" garante que sempre temos um dict
    # pra chamar .get() com segurança, mesmo com um corpo de requisição hostil/quebrado.
    data = request.get_json(silent=True) or {}
    telefone_raw = data.get("telefone", "").strip()[:30]  # telefone real nunca passa de ~15 dígitos
    mensagem = data.get("mensagem", "").strip()[:2000]  # limite defensivo contra payloads abusivos
    if not telefone_raw or not mensagem:
        return jsonify({"erro": "dados incompletos"}), 400

    try:
        cliente = buscar_ou_criar_cliente(telefone_raw)
        conversa = buscar_ou_criar_conversa(cliente["id"])

        # Proteção contra reprocessar a mesma mensagem duas vezes (webhook duplicado).
        # Só entra em ação se o n8n estiver mandando o "mensagem_id" (opcional).
        mensagem_id = (data.get("mensagem_id") or "")[:100] or None
        resposta_duplicada = verificar_mensagem_duplicada(mensagem_id, conversa["id"])
        if resposta_duplicada is not None:
            logger.warning(f"Mensagem duplicada detectada (id={mensagem_id}) - devolvendo resposta anterior sem reprocessar")
            return jsonify({"ok": True, "resposta": resposta_duplicada, "duplicado": True})

        timestamp_minha_mensagem = salvar_mensagem(conversa["id"], "cliente", mensagem)

        # Proteção contra mensagens em sequência rápida (comum no WhatsApp: o cliente
        # manda 2-3 mensagens curtas seguidas, cada uma seria processada e respondida
        # SEPARADAMENTE, gerando confirmações duplicadas/confusas). Espera um pouco e
        # confere se já chegou algo mais novo - se sim, deixa a mensagem mais recente
        # "vencer" e responder por todas juntas (ela vai ver esta mensagem no histórico).
        time.sleep(3)
        if existe_mensagem_cliente_mais_nova(conversa["id"], timestamp_minha_mensagem):
            logger.info(
                "Mensagem superada por outra mais recente - não respondendo separadamente",
                extra={"evento": "mensagem_superada", "conversa_id": conversa["id"]},
            )
            return jsonify({"ok": True, "resposta": "", "superada": True})

        historico = obter_historico(conversa["id"])

        # Monta o histórico como mensagens de verdade (user/assistant), não como um texto único.
        # Isso é o formato correto da API de mensagens da Claude, e permite usar tool use.
        messages = []
        for m in historico[:-1]:
            role = "user" if m["remetente"] == "cliente" else "assistant"
            messages.append({"role": role, "content": m["conteudo"]})
        messages.append({"role": "user", "content": mensagem})

        # Se não há nenhuma mensagem anterior nesta conversa, é o primeiro contato -
        # a IA deve incluir a nota curta de privacidade (LGPD) na resposta.
        system_prompt_final = SYSTEM_PROMPT
        if not historico[:-1]:
            system_prompt_final += "\n\nCONTEXTO: esta é a PRIMEIRA mensagem desta conversa - inclua a nota curta de privacidade no final da sua resposta, como instruído acima."

        # Injeta a memória estruturada do pedido (persistida no banco) diretamente no
        # contexto - assim a IA tem uma fonte confiável do que já foi informado, em vez
        # de precisar reler e "adivinhar" a partir do texto cru da conversa toda vez.
        estado_atual = obter_estado_pedido(conversa["id"])
        if estado_atual.get("itens"):
            system_prompt_final += "\n\nESTADO ATUAL DO PEDIDO (já confirmado nesta conversa - NÃO pergunte de novo o que já está aqui):\n" + json.dumps(estado_atual, ensure_ascii=False)

        resposta = gerar_resposta(messages, system_prompt_final, cliente, conversa)

        salvar_mensagem(conversa["id"], "ia", resposta)
        lead = calcular_score(conversa["id"], cliente["id"])
        # NOTA: o envio da mensagem ao cliente é feito pelo n8n (HTTP Request1),
        # por isso NÃO chamamos enviar_whatsapp() aqui para o cliente (evita duplicar).
        if lead["score"] >= 80:
            notificar_proprietario(cliente, lead["score"], conversa["id"])

        return jsonify({"ok": True, "resposta": resposta, "score": lead["score"], "categoria": lead["categoria"]})
    except Exception as e:
        # Mesmo com um erro totalmente inesperado, o cliente NUNCA pode ficar em silêncio -
        # por isso devolvemos 200 (não 500) com uma resposta de reserva, pra o n8n continuar
        # o fluxo normalmente e mandar essa mensagem pro WhatsApp. O erro real fica no log,
        # não escondido do desenvolvedor, só escondido do cliente.
        logger.error(f"Erro inesperado no /webhook: {e}")
        return jsonify({
            "ok": False,
            "resposta": "Desculpa, tive um probleminha aqui rapidinho 🙏 Pode repetir sua última mensagem?",
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


@bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})
