"""
As ferramentas (tool use) que a IA pode chamar durante a conversa: atualizar a memória
estruturada do pedido, consultar o mínimo, calcular o orçamento oficial, fechar o
pedido, transferir para um consultor humano, e tratar pedidos de privacidade (LGPD).

Cada "executar_..." é a função Python de verdade que roda quando a IA chama a
ferramenta correspondente. As notificações (fechar_pedido, transferir_para_consultor,
solicitar_privacidade) são disparadas de dentro do loop de ferramentas em app/ia.py,
não aqui - este módulo cuida só do cálculo/validação dos dados do pedido.
"""
from app.config import logger, PRODUTOS_VALIDOS, MATERIAIS_VALIDOS, CORES_PRODUTO_VALIDAS
from app.precos import (
    ajustar_tamanho, espessura_mais_proxima, calcular_preco, calcular_pedido_minimo,
    processar_item_pedido,
)
from app.database import salvar_estado_pedido


def executar_atualizar_pedido(conversa_id, entrada):
    """Executa a ferramenta 'atualizar_pedido': é o coração da memória estruturada da
    conversa. Recebe TODOS os itens que a IA já sabe sobre o pedido (um ou vários
    tamanhos/produtos), valida/ajusta cada um, salva como estado da conversa, e
    devolve o que falta em cada item - assim a IA nunca precisa perguntar de novo
    algo que já foi respondido."""
    try:
        itens_entrada = entrada.get("itens") or []
        resultados = [processar_item_pedido(it) for it in itens_entrada]
        estado = {"itens": [r["item"] for r in resultados], "observacoes": entrada.get("observacoes", "")}
        salvar_estado_pedido(conversa_id, estado)
        return {
            "itens": [
                {**r["item"], "ajustes_feitos": r["ajustes"], "faltando": r["faltando"],
                 "completo": r["completo"], "preco_preview": r["preco_preview"]}
                for r in resultados
            ],
            "observacoes": estado["observacoes"],
            "total_itens": len(resultados),
            "itens_completos": sum(1 for r in resultados if r["completo"]),
        }
    except Exception as e:
        logger.error(f"Erro na ferramenta atualizar_pedido: {e}")
        return {"erro": "Não foi possível atualizar o pedido agora. Continue a conversa normalmente e tente de novo em seguida."}


def executar_consultar_pedido_minimo(entrada):
    """Executa a ferramenta 'consultar_pedido_minimo': ajusta tamanho/espessura para valores
    tecnicamente válidos e calcula o pedido mínimo real dessa combinação."""
    try:
        produto = entrada.get("produto")
        largura = float(entrada["largura"])
        altura = float(entrada["altura"])
        espessura_pedida = float(entrada["espessura"])
        cores_n = int(entrada["cores_n"])

        largura, altura, ajustes = ajustar_tamanho(produto, largura, altura, cores_n)
        espessura = espessura_mais_proxima(espessura_pedida, produto)
        if abs(espessura - espessura_pedida) > 1e-6:
            ajustes.append(f"espessura ajustada de {espessura_pedida:g}mm para {espessura:g}mm (opção disponível para este produto)")

        minimo = calcular_pedido_minimo(largura, altura, espessura, cores_n)
        return {
            "largura_usada": largura, "altura_usada": altura, "espessura_usada": espessura,
            "ajustes_feitos": ajustes,
            "pedido_minimo_milheiros": minimo["milheiros_min"] if minimo else None,
            "pedido_minimo_kg": minimo["kg_min"] if minimo else None,
        }
    except Exception as e:
        logger.error(f"Erro na ferramenta consultar_pedido_minimo: {e}")
        return {"erro": "Não foi possível calcular o mínimo para esses dados. Peça para o cliente confirmar produto, tamanho e espessura novamente."}


def executar_calcular_orcamento(entrada):
    """Executa a ferramenta 'calcular_orcamento': é a ÚNICA forma pela qual um preço final
    chega até o cliente. A IA nunca calcula preço sozinha - só usa o que esta função devolve."""
    try:
        produto = entrada.get("produto")
        material = entrada.get("material") or "Virgem BD"
        if material not in MATERIAIS_VALIDOS:
            material = "Virgem BD"
        cor_produto = entrada.get("cor_produto") or "Transparente"
        if cor_produto not in CORES_PRODUTO_VALIDAS:
            cor_produto = "Transparente"
        largura = float(entrada["largura"])
        altura = float(entrada["altura"])
        espessura_pedida = float(entrada["espessura"])
        cores_n = int(entrada["cores_n"])
        impressao = entrada.get("impressao") or "FRENTE"
        imp_map = "IMPRESSÃO FRENTE / VERSO" if impressao == "FRENTE_VERSO" else "IMPRESSÃO FRENTE"
        milheiros = float(entrada["milheiros"])

        largura, altura, ajustes = ajustar_tamanho(produto, largura, altura, cores_n)
        espessura = espessura_mais_proxima(espessura_pedida, produto)
        if abs(espessura - espessura_pedida) > 1e-6:
            ajustes.append(f"espessura ajustada de {espessura_pedida:g}mm para {espessura:g}mm (opção disponível para este produto)")

        calc = calcular_preco(produto, material, largura, altura, cores_n, imp_map, milheiros, espessura=espessura)

        if not calc["atende_minimo"]:
            return {
                "erro": "peso abaixo do mínimo exigido para esta combinação",
                "pedido_minimo_milheiros": calc["pedido_minimo_milheiros"],
                "peso_calculado_kg": calc["peso_total_kg"],
                "peso_minimo_kg": calc["pedido_minimo_kg"],
                "instrucao": "Explique ao cliente que não é possível fechar nesse peso e peça para aumentar a quantidade para o mínimo informado.",
            }

        return {
            "produto": produto, "material": material, "cor_produto": cor_produto,
            "largura_usada": largura, "altura_usada": altura, "espessura_usada": espessura,
            "cores_n": cores_n, "impressao": impressao, "milheiros": milheiros,
            "ajustes_feitos": ajustes,
            "preco_por_milheiro": calc["milheiro"],
            "preco_total": calc["total"],
            "peso_total_kg": calc["peso_total_kg"],
        }
    except Exception as e:
        logger.error(f"Erro na ferramenta calcular_orcamento: {e}")
        return {"erro": "Não foi possível calcular o preço para esta combinação agora. Diga ao cliente que vai confirmar com a equipe e retornar em breve. NÃO informe nenhum valor."}


TOOLS = [
    {
        "name": "atualizar_pedido",
        "description": "Chame esta ferramenta SEMPRE que o cliente informar qualquer dado novo sobre o pedido - mesmo que venha tudo numa mensagem só, ou aos poucos. Pode incluir MAIS DE UM item se o cliente mencionar vários tamanhos ou produtos na mesma mensagem (ex: '30x40, 40x50 e 50x50'). Envie a lista COMPLETA de itens que você já conhece desta conversa (os de antes + os novos) - esta ferramenta substitui o estado anterior pelo que você enviar. O resultado te diz o que falta em cada item, então você nunca precisa perguntar de novo o que já foi informado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "itens": {
                    "type": "array",
                    "description": "Lista de TODOS os itens do pedido que você já sabe (um item = um produto+tamanho). Inclua os já conhecidos de mensagens anteriores MAIS os novos desta mensagem.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "produto": {"type": "string", "enum": PRODUTOS_VALIDOS},
                            "material": {"type": "string", "enum": MATERIAIS_VALIDOS},
                            "cor_produto": {"type": "string", "enum": CORES_PRODUTO_VALIDAS},
                            "largura": {"type": "number", "description": "largura em cm"},
                            "altura": {"type": "number", "description": "altura em cm"},
                            "espessura": {"type": "number", "description": "espessura em mm"},
                            "cores_n": {"type": "integer", "description": "número de cores de impressão (0 se sem impressão)"},
                            "impressao": {"type": "string", "enum": ["FRENTE", "FRENTE_VERSO"]},
                            "milheiros": {"type": "number", "description": "quantidade em milheiros (mil unidades)"},
                        },
                    },
                },
                "observacoes": {"type": "string", "description": "Qualquer observação extra relevante sobre o pedido (opcional)."},
            },
            "required": ["itens"],
        },
    },
    {
        "name": "consultar_pedido_minimo",
        "description": "Consulta o pedido mínimo (em mil unidades) para uma combinação de produto+tamanho+espessura+cores, ANTES de perguntar a quantidade ao cliente. Também ajusta tamanho/espessura para os valores tecnicamente disponíveis, se necessário. Use assim que tiver produto, largura, altura, espessura e número de cores.",
        "input_schema": {
            "type": "object",
            "properties": {
                "produto": {"type": "string", "enum": PRODUTOS_VALIDOS},
                "largura": {"type": "number", "description": "largura em cm"},
                "altura": {"type": "number", "description": "altura em cm"},
                "espessura": {"type": "number", "description": "espessura em mm"},
                "cores_n": {"type": "integer", "description": "número de cores de impressão (0 se sem impressão)"},
            },
            "required": ["produto", "largura", "altura", "espessura", "cores_n"],
        },
    },
    {
        "name": "calcular_orcamento",
        "description": "Calcula o preço OFICIAL e final do pedido. É a única forma válida de informar preço ao cliente - NUNCA calcule ou estime um valor por conta própria. Use somente quando já tiver TODAS as informações: produto, material, tamanho, espessura, cores e quantidade em milheiros.",
        "input_schema": {
            "type": "object",
            "properties": {
                "produto": {"type": "string", "enum": PRODUTOS_VALIDOS},
                "material": {"type": "string", "enum": MATERIAIS_VALIDOS, "description": "usa 'Virgem BD' se o cliente não especificou"},
                "cor_produto": {"type": "string", "enum": CORES_PRODUTO_VALIDAS, "description": "cor da sacola em si (não afeta o preço, é só informativo). Use 'Transparente' se o cliente não especificou."},
                "largura": {"type": "number", "description": "largura em cm"},
                "altura": {"type": "number", "description": "altura em cm"},
                "espessura": {"type": "number", "description": "espessura em mm"},
                "cores_n": {"type": "integer", "description": "número de cores de impressão (0 se sem impressão)"},
                "impressao": {"type": "string", "enum": ["FRENTE", "FRENTE_VERSO"]},
                "milheiros": {"type": "number", "description": "quantidade pedida, em milheiros (mil unidades)"},
            },
            "required": ["produto", "material", "largura", "altura", "espessura", "cores_n", "impressao", "milheiros"],
        },
    },
    {
        "name": "fechar_pedido",
        "description": "Chame esta ferramenta assim que o cliente confirmar claramente que quer fechar/prosseguir com o pedido (ex.: respondeu 'sim' depois de você perguntar 'Posso gerar a proposta?'). Isso avisa o consultor humano para finalizar a venda. Só chame depois de já ter apresentado um preço calculado (via calcular_orcamento) nesta conversa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "resumo": {
                    "type": "string",
                    "description": "Resumo em texto corrido do pedido fechado: produto, tamanho, material, espessura, cores, quantidade em mil unidades e o preço total combinado.",
                },
            },
            "required": ["resumo"],
        },
    },
    {
        "name": "transferir_para_consultor",
        "description": "Chame esta ferramenta quando você não souber responder algo importante ao cliente, quando a pergunta estiver fora do que você sabe (fora de vendas de sacolas/sacos plásticos), ou quando o cliente pedir claramente para falar com uma pessoa/atendente humano. Isso avisa um consultor humano para assumir a conversa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Resumo breve do que o cliente perguntou ou precisa, que você não conseguiu resolver sozinho.",
                },
            },
            "required": ["motivo"],
        },
    },
    {
        "name": "solicitar_privacidade",
        "description": "Chame esta ferramenta sempre que o cliente perguntar sobre os dados pessoais dele (o que guardamos, por quê), ou pedir para ver, corrigir, ou APAGAR/EXCLUIR os dados dele, ou se opuser ao uso dos dados (direitos da LGPD). NÃO apague nada sozinho - isso avisa um humano que vai executar o pedido com segurança.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": ["acesso", "correcao", "exclusao", "duvida"], "description": "acesso = quer saber o que temos; correcao = quer corrigir algo; exclusao = quer apagar os dados; duvida = pergunta geral sobre privacidade"},
                "detalhe": {"type": "string", "description": "O que exatamente o cliente pediu, em texto corrido."},
            },
            "required": ["tipo", "detalhe"],
        },
    },
]
