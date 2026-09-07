"""
As rotas HTTP do robô: /webhook (recebe mensagens do n8n), /manutencao/limpeza
(limpeza periódica de dados antigos, LGPD) e /health (checagem de saúde do serviço).

Este módulo é propositalmente enxuto: ele só orquestra a chamada dos outros módulos
(database, whatsapp, ia) - a lógica de negócio de verdade vive lá, não aqui.
"""
import json
import time

from flask import Blueprint, request, jsonify

from app.config import logger, WEBHOOK_SECRET, limiter, correlation_id_var
from app.database import (
    buscar_ou_criar_cliente, buscar_ou_criar_conversa, verificar_mensagem_duplicada,
    salvar_mensagem, obter_historico, obter_estado_pedido, calcular_score,
    limpar_dados_antigos, existe_mensagem_cliente_mais_nova, verificar_conexao_db,
    buscar_conversas_para_reengajar,
)
from app.whatsapp import notificar_proprietario, reengajar_cliente
from app.ia import gerar_resposta, SYSTEM_PROMPT
from app.precos import recarregar_tabela_precos

bp = Blueprint("webhook", __name__)

# Palavras que, numa resposta curta logo após um orçamento, indicam recusa.
# Ficam aqui (não dentro da função) para não recriar a lista a cada requisição.
PALAVRAS_RECUSA_ORCAMENTO = ["não", "nao", "deixa", "desisto", "sem interesse", "outra hora", "obrigad"]


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
    # "or ''" (em vez de só o default do .get) protege contra o campo vir presente
    # mas com valor null - nesse caso data.get(..., "") ainda devolveria None, e
    # None.strip() quebrava o webhook inteiro sem resposta nenhuma pro cliente.
    telefone_raw = (data.get("telefone") or "").strip()[:30]  # telefone real nunca passa de ~15 dígitos
    mensagem = (data.get("mensagem") or "").strip()[:2000]  # limite defensivo contra payloads abusivos
    if not telefone_raw:
        return jsonify({"erro": "dados incompletos"}), 400

    if not mensagem:
        # O cliente mandou algo que não é texto puro (áudio, imagem, figurinha, documento,
        # etc.) - hoje ainda não sabemos processar esses formatos, e o n8n manda o campo
        # "mensagem" vazio nesses casos. ANTES: isso batia no "if not mensagem" acima e
        # devolvia erro 400 - o cliente ficava em silêncio total, sem entender por quê.
        # AGORA: avisamos e pedimos pra escrever em texto, e registramos no histórico.
        try:
            cliente = buscar_ou_criar_cliente(telefone_raw)
            conversa = buscar_ou_criar_conversa(cliente["id"])
            correlation_id_var.set(str(conversa["id"])[:8])
            salvar_mensagem(conversa["id"], "cliente", "[cliente enviou uma mídia - áudio, imagem, figurinha ou documento]")
            resposta = "Recebi algo aqui, mas ainda não consigo ouvir áudio nem ver imagem/figurinha 🙏 Pode me escrever em texto o que você precisa?"
            salvar_mensagem(conversa["id"], "ia", resposta)
            return jsonify({"ok": True, "resposta": resposta})
        except Exception as e:
            logger.error(f"Erro ao tratar mensagem não-textual: {e}")
            return jsonify({
                "ok": False,
                "resposta": "Recebi algo aqui, mas ainda não consigo ouvir áudio nem ver imagem/figurinha 🙏 Pode me escrever em texto o que você precisa?",
            }), 200

    try:
        cliente = buscar_ou_criar_cliente(telefone_raw)
        conversa = buscar_ou_criar_conversa(cliente["id"])

        # A partir daqui, TODO log (deste módulo ou de qualquer outro - database,
        # whatsapp, ia) já sai marcado com o conversa_id. Isso permite filtrar os
        # logs de UM cliente específico do início ao fim, mesmo com várias
        # conversas acontecendo ao mesmo tempo no mesmo processo do servidor.
        correlation_id_var.set(str(conversa["id"])[:8])

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
        # Isso e o estado do pedido vão num bloco SEPARADO do SYSTEM_PROMPT (que é fixo
        # e cacheável) - assim a parte que nunca muda não perde o cache por causa da
        # parte que muda a cada mensagem.
        partes_contexto_extra = []
        if not historico[:-1]:
            partes_contexto_extra.append(
                "CONTEXTO: esta é a PRIMEIRA mensagem desta conversa - inclua a nota curta de privacidade no final da sua resposta, como instruído acima."
            )

        # Injeta a memória estruturada do pedido (persistida no banco) diretamente no
        # contexto - assim a IA tem uma fonte confiável do que já foi informado, em vez
        # de precisar reler e "adivinhar" a partir do texto cru da conversa toda vez.
        estado_atual = obter_estado_pedido(conversa["id"])
        if estado_atual.get("itens"):
            partes_contexto_extra.append(
                "ESTADO ATUAL DO PEDIDO (já confirmado nesta conversa - NÃO pergunte de novo o que já está aqui):\n"
                + json.dumps(estado_atual, ensure_ascii=False)
            )

        # Detecta se a mensagem do cliente é uma recusa curta logo após você ter
        # apresentado um orçamento. Fica aqui (contexto dinâmico, não no SYSTEM_PROMPT
        # fixo) de propósito: essa instrução só custa tokens/atenção da IA nas conversas
        # onde realmente se aplica, em vez de inflar o prompt fixo (cacheado, pago em
        # toda chamada) com mais uma regra permanente para um caso específico.
        if len(historico) >= 2 and historico[-2]["remetente"] == "ia" and "Orçamento Plastcustom" in historico[-2]["conteudo"]:
            mensagem_curta = len(mensagem) < 60
            parece_recusa = mensagem_curta and any(p in mensagem.lower() for p in PALAVRAS_RECUSA_ORCAMENTO)
            if parece_recusa:
                partes_contexto_extra.append(
                    "CONTEXTO: você acabou de apresentar um orçamento e o cliente respondeu recusando "
                    "de forma direta e curta. NÃO encerre a conversa educadamente sem reagir - pergunte "
                    "o motivo de forma leve (valor, quantidade, prazo?) e ofereça pelo menos UMA "
                    "alternativa concreta (menos cores, mais quantidade para baixar o preço por unidade, "
                    "outro material, ou segurar o preço por 7 dias) antes de aceitar como resposta final."
                )

        contexto_extra = "\n\n".join(partes_contexto_extra)
        resposta = gerar_resposta(messages, contexto_extra, cliente, conversa)
        if not resposta:
            # Rede de segurança: a coluna "conteudo" no banco não aceita valor vazio/nulo.
            # Sem isso, se gerar_resposta devolvesse "" por qualquer motivo, o INSERT
            # falhava, o cliente ficava sem resposta, e o erro só aparecia no log.
            logger.warning("IA devolveu resposta vazia - usando frase de reserva")
            resposta = "Desculpa, tive um probleminha aqui rapidinho 🙏 Pode repetir sua última mensagem?"

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
