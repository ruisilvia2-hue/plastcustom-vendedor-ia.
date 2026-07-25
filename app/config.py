"""
Configuração central: variáveis de ambiente, constantes de negócio (materiais, cores,
produtos válidos) e o logger compartilhado por todos os outros módulos.

Nenhum outro módulo deve ler os.environ diretamente - tudo passa por aqui, então dá
pra saber, num único lugar, tudo que o projeto precisa ter configurado pra rodar.
"""
import os
import logging

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("vendedor_ia")

# ============================================================
# VARIÁVEIS DE AMBIENTE (credenciais e configuração de infraestrutura)
# ============================================================
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
EVOLUTION_URL = os.environ["EVOLUTION_API_URL"]
EVOLUTION_KEY = os.environ["EVOLUTION_API_KEY"]
PROPRIETARIO = os.environ["PROPRIETARIO_TELEFONE"]
CONSULTOR_TELEFONE = os.environ["CONSULTOR_TELEFONE"]  # recebe o resumo quando um pedido é fechado
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]  # segredo compartilhado com o n8n para autenticar o /webhook

# ============================================================
# CAMINHOS DE ARQUIVO
# ============================================================
# BASE_DIR = pasta raiz do projeto (onde ficam o Dockerfile, requirements.txt, e o
# Plastcustom_Orcamento.html) - calculado subindo um nível a partir de app/config.py,
# então funciona independente de onde o processo é iniciado.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMINHO_CALCULADORA = os.path.join(BASE_DIR, "Plastcustom_Orcamento.html")

# ============================================================
# CONSTANTES DE NEGÓCIO (catálogo válido)
# ============================================================
PRODUTOS_VALIDOS = ["Sacola Camiseta", "Sacola Vazada", "Saco Impresso Solda Fundo", "Saco com Aba"]
MATERIAIS_VALIDOS = ["Virgem BD", "Virgem AD", "Reciclado Cor", "Reciclado Sem Cor", "Polipropileno (PP)"]
# Cor do PRODUTO (a cor da sacola em si) - diferente de "cores de impressão" (a logomarca).
# Não afeta o preço, é só uma característica visual do pedido.
CORES_PRODUTO_VALIDAS = ["Branca", "Preta", "Azul", "Vermelha", "Verde", "Amarela", "Laranja", "Cinza", "Transparente", "Natural"]

# ============================================================
# PONTUAÇÃO DE LEAD (palavras-chave que aumentam/diminuem o "score" de interesse)
# ============================================================
SINAIS = {
    "perguntou_preco": (["preço", "valor", "custa", "quanto", "tabela"], 20),
    "perguntou_prazo": (["prazo", "entrega", "quando", "dias"], 15),
    "escolheu_modelo": (["camiseta", "vazada", "impresso", "aba", "sacola", "saco"], 25),
    "escolheu_tamanho": (["30x40", "40x50", "50x60", "60x80", "80x100", "tamanho", "medida"], 20),
    "escolheu_quantidade": (["mil", "unidades", "quantidade"], 30),
    "pediu_orcamento": (["orçamento", "proposta", "cotação", "calcul"], 35),
    "tem_empresa": (["empresa", "loja", "mercado", "farmácia", "padaria", "cnpj", "supermercado"], 15),
    "mandou_logo": (["logo", "logomarca", "arquivo", "arte"], 40),
    "confirmou_pedido": (["confirmo", "quero fechar", "fechado", "pode gerar", "sim pode", "fecha pedido", "fecha o pedido"], 50),
    "vou_pensar": (["pensar", "depois", "talvez", "não sei"], -10),
    "ta_caro": (["caro", "salgado", "muito caro"], -15),
}
