"""
Testes automatizados da lógica de preço/tamanho/espessura do Vendedor IA.

COMO RODAR:
    python3 -m unittest test_pricing.py -v

Se algum teste falhar depois de uma mudança no código, é sinal de que algo no
cálculo de preço, tamanho ou espessura mudou de comportamento sem querer.
Esses valores "esperados" foram conferidos manualmente contra a calculadora
oficial da Plastcustom (Plastcustom_Orcamento.html) durante o desenvolvimento.
"""
import os
import unittest

# Variáveis de ambiente falsas só pra permitir importar o pacote sem precisar
# de credenciais reais nem de conexão de verdade com banco/IA/WhatsApp.
os.environ.setdefault("CLAUDE_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgres://test:test@localhost/test")
os.environ.setdefault("EVOLUTION_API_URL", "http://test.invalid")
os.environ.setdefault("EVOLUTION_API_KEY", "test-key")
os.environ.setdefault("PROPRIETARIO_TELEFONE", "5500000000000")
os.environ.setdefault("CONSULTOR_TELEFONE", "5500000000000")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

# Os testes de cálculo importam direto do módulo de preços (não precisam do Flask
# nem do banco de verdade) - mais rápido e mais isolado.
from app import precos as app
from app import tools as tools_app


class TestCalculoPreco(unittest.TestCase):
    def test_caso_validado_contra_calculadora_oficial(self):
        """Sacola Camiseta 30x40, Polietileno PEAD (Virgem AD), 0,009mm, 4 cores, 14 mil unidades.
        Valores conferidos manualmente na calculadora oficial: R$37,83/kg, R$408,56/mil, R$5.719,90 total."""
        r = app.calcular_preco("Sacola Camiseta", "Virgem AD", 30, 40, 4, "IMPRESSÃO FRENTE", 14, espessura=0.009)
        self.assertAlmostEqual(r["preco_kg"], 37.83, places=2)
        self.assertAlmostEqual(r["milheiro"], 408.56, places=2)
        self.assertAlmostEqual(r["total"], 5719.90, places=1)
        self.assertAlmostEqual(r["peso_total_kg"], 151.2, places=1)

    def test_sem_impressao_aplica_desconto(self):
        """Sem impressão (cores_n=0) tem desconto de R$2/kg, exceto para PP."""
        com_impressao = app.calcular_preco("Sacola Camiseta", "Virgem BD", 30, 40, 1, "IMPRESSÃO FRENTE", 30, espessura=0.008)
        sem_impressao = app.calcular_preco("Sacola Camiseta", "Virgem BD", 30, 40, 0, "IMPRESSÃO FRENTE", 30, espessura=0.008)
        self.assertLess(sem_impressao["preco_kg"], com_impressao["preco_kg"])

    def test_combinacao_sem_preco_na_tabela_gera_erro_controlado(self):
        """Uma combinação inválida (material inexistente) deve levantar um erro claro,
        nunca devolver um preço silenciosamente errado."""
        with self.assertRaises(ValueError):
            app.calcular_preco("Sacola Camiseta", "Material Inexistente", 30, 40, 1, "IMPRESSÃO FRENTE", 30)


class TestAjusteDeTamanho(unittest.TestCase):
    def test_tamanho_valido_nao_e_alterado(self):
        """Sacola Camiseta 35x55 é um tamanho válido de verdade - não deve ser 'corrigido'."""
        largura, altura, ajustes = app.ajustar_tamanho("Sacola Camiseta", 35, 55, 3)
        self.assertEqual(largura, 35)
        self.assertEqual(altura, 55)
        self.assertEqual(ajustes, [])

    def test_largura_fora_da_lista_e_ajustada(self):
        """Sacola Camiseta só aceita larguras específicas (30,35,38,40,45,50,55,60,65,70,75,80,85,90)."""
        largura, altura, ajustes = app.ajustar_tamanho("Sacola Camiseta", 37, 40, 1)
        self.assertIn(largura, app.LARGURAS_SACOLA_CAMISETA_PERMITIDAS)
        self.assertTrue(len(ajustes) >= 1)

    def test_todas_larguras_permitidas_sao_aceitas_sem_ajuste(self):
        for largura in app.LARGURAS_SACOLA_CAMISETA_PERMITIDAS:
            with self.subTest(largura=largura):
                nova = app.largura_camiseta_mais_proxima(largura)
                self.assertEqual(nova, largura)


class TestEspessura(unittest.TestCase):
    def test_espessura_valida_nao_e_alterada(self):
        self.assertEqual(app.espessura_mais_proxima(0.008, "Sacola Vazada"), 0.008)

    def test_espessura_invalida_e_ajustada_para_a_mais_proxima_do_produto(self):
        # 0,070mm não existe para Sacola Vazada (máximo é 0,045mm)
        ajustada = app.espessura_mais_proxima(0.070, "Sacola Vazada")
        self.assertIn(ajustada, app.ESPESSURAS_POR_PRODUTO["Sacola Vazada"])
        self.assertLessEqual(ajustada, 0.045)

    def test_cada_produto_tem_sua_propria_lista(self):
        # Sacola Camiseta e Sacola Vazada têm listas de espessura diferentes
        self.assertNotEqual(
            app.ESPESSURAS_POR_PRODUTO["Sacola Camiseta"],
            app.ESPESSURAS_POR_PRODUTO["Sacola Vazada"],
        )


class TestPedidoMinimo(unittest.TestCase):
    def test_minimo_nao_e_fixo_varia_por_peso(self):
        """O pedido mínimo tem que mudar conforme a espessura - não pode ser sempre 30 mil."""
        minimo_fino = app.calcular_pedido_minimo(30, 40, 0.003, 1)
        minimo_grosso = app.calcular_pedido_minimo(30, 40, 0.009, 1)
        self.assertNotEqual(minimo_fino["milheiros_min"], minimo_grosso["milheiros_min"])
        # espessura mais fina -> pesa menos por unidade -> precisa de MAIS unidades pra bater o peso mínimo
        self.assertGreater(minimo_fino["milheiros_min"], minimo_grosso["milheiros_min"])

    def test_minimo_com_impressao_maior_que_sem_impressao(self):
        com = app.calcular_pedido_minimo(30, 40, 0.008, 3)   # 150kg mínimo
        sem = app.calcular_pedido_minimo(30, 40, 0.008, 0)   # 100kg mínimo
        self.assertGreater(com["milheiros_min"], sem["milheiros_min"])


class TestFerramentasDaIA(unittest.TestCase):
    """Testa as funções que a IA chama via tool use - o ponto de entrada real em produção."""

    def test_calcular_orcamento_caso_normal(self):
        resultado = tools_app.executar_calcular_orcamento({
            "produto": "Sacola Vazada", "material": "Virgem BD", "largura": 40, "altura": 50,
            "espessura": 0.008, "cores_n": 3, "impressao": "FRENTE", "milheiros": 30,
        })
        self.assertNotIn("erro", resultado)
        self.assertIn("preco_total", resultado)
        self.assertGreater(resultado["preco_total"], 0)

    def test_calcular_orcamento_abaixo_do_minimo_retorna_fora_da_faixa(self):
        resultado = tools_app.executar_calcular_orcamento({
            "produto": "Sacola Vazada", "material": "Virgem BD", "largura": 40, "altura": 50,
            "espessura": 0.008, "cores_n": 3, "impressao": "FRENTE", "milheiros": 1,
        })
        self.assertEqual(resultado.get("erro"), "fora_da_faixa")
        self.assertIn("mensagem", resultado)
        self.assertIn("sugestoes", resultado)
        self.assertNotIn("preco_total", resultado)

    def test_calcular_orcamento_dado_malformado_retorna_valor_invalido(self):
        """Se a IA mandar um valor no formato errado, tem que devolver erro tratado,
        nunca deixar uma exceção crua estourar (que derrubaria a resposta ao cliente)."""
        resultado = tools_app.executar_calcular_orcamento({
            "produto": "Sacola Vazada", "largura": "não é um número", "altura": 50,
            "espessura": 0.008, "cores_n": 3, "impressao": "FRENTE", "milheiros": 30,
        })
        self.assertEqual(resultado.get("erro"), "valor_invalido")
        self.assertIn("mensagem", resultado)

    def test_calcular_orcamento_sem_campos_obrigatorios_retorna_dados_incompletos(self):
        resultado = tools_app.executar_calcular_orcamento({
            "produto": "Sacola Vazada", "largura": 40, "altura": 50,
            # faltando: espessura, cores_n, milheiros
        })
        self.assertEqual(resultado.get("erro"), "dados_incompletos")
        self.assertIn("mensagem", resultado)
        self.assertEqual(set(resultado.get("campos_faltando", [])), {"espessura", "cores_n", "milheiros"})

    def test_calcular_orcamento_produto_desconhecido_retorna_valor_invalido(self):
        resultado = tools_app.executar_calcular_orcamento({
            "produto": "Sacola Que Não Existe", "largura": 40, "altura": 50,
            "espessura": 0.008, "cores_n": 3, "impressao": "FRENTE", "milheiros": 30,
        })
        self.assertEqual(resultado.get("erro"), "valor_invalido")
        self.assertIn("campos_faltando", resultado)

    def test_consultar_pedido_minimo_caso_normal(self):
        resultado = tools_app.executar_consultar_pedido_minimo({
            "produto": "Sacola Camiseta", "largura": 35, "altura": 55, "espessura": 0.008, "cores_n": 3,
        })
        self.assertNotIn("erro", resultado)
        self.assertIn("pedido_minimo_milheiros", resultado)


class TestMemoriaEstruturadaDoPedido(unittest.TestCase):
    """Testa a peça central da nova arquitetura: processar_item_pedido (usada por
    atualizar_pedido) - suporte a múltiplos itens, sem repetir perguntas já respondidas."""

    def test_item_completo_gera_preco_preview(self):
        r = app.processar_item_pedido({
            "produto": "Sacola Vazada", "material": "Virgem BD", "cor_produto": "Azul",
            "largura": 40, "altura": 50, "espessura": 0.008, "cores_n": 3,
            "impressao": "FRENTE", "milheiros": 30,
        })
        self.assertTrue(r["completo"])
        self.assertEqual(r["faltando"], [])
        self.assertIsNotNone(r["preco_preview"])
        self.assertGreater(r["preco_preview"]["preco_total"], 0)

    def test_item_incompleto_lista_o_que_falta(self):
        r = app.processar_item_pedido({"produto": "Sacola Vazada", "largura": 40, "altura": 50})
        self.assertFalse(r["completo"])
        self.assertIn("espessura", r["faltando"])
        self.assertIn("cores_n", r["faltando"])
        self.assertIn("milheiros", r["faltando"])
        self.assertIsNone(r["preco_preview"])

    def test_item_ajusta_tamanho_fora_de_faixa_automaticamente(self):
        # Sacola Camiseta só aceita larguras específicas - 37 não é uma delas
        r = app.processar_item_pedido({
            "produto": "Sacola Camiseta", "largura": 37, "altura": 40, "cores_n": 1,
        })
        self.assertIn(r["item"]["largura"], app.LARGURAS_SACOLA_CAMISETA_PERMITIDAS)
        self.assertTrue(len(r["ajustes"]) >= 1)

    def test_multiplos_itens_sao_processados_independentemente(self):
        """Simula o cenário pedido pelo usuário: cliente informa vários tamanhos numa mensagem só."""
        entrada = {
            "itens": [
                {"produto": "Sacola Vazada", "material": "Virgem BD", "largura": 30, "altura": 40,
                 "espessura": 0.008, "cores_n": 0, "impressao": "FRENTE", "milheiros": 30},
                {"produto": "Sacola Vazada", "material": "Virgem BD", "largura": 40, "altura": 50,
                 "espessura": 0.008, "cores_n": 0, "impressao": "FRENTE", "milheiros": 30},
                {"produto": "Sacola Vazada", "material": "Virgem BD", "largura": 50, "altura": 50,
                 "espessura": 0.008, "cores_n": 0, "impressao": "FRENTE", "milheiros": 30},
            ]
        }
        resultado = tools_app.executar_atualizar_pedido(conversa_id="00000000-0000-0000-0000-000000000000", entrada=entrada)
        self.assertEqual(resultado["total_itens"], 3)
        # cada item tem preço calculado de forma independente
        precos = [it["preco_preview"]["preco_total"] for it in resultado["itens"]]
        self.assertEqual(len(set(precos)), 3)  # tamanhos diferentes -> preços diferentes

    def test_material_invalido_e_descartado_sem_quebrar(self):
        r = app.processar_item_pedido({"produto": "Sacola Vazada", "material": "Material Inventado", "largura": 40, "altura": 50})
        self.assertIsNone(r["item"]["material"])  # não aceita valor fora da lista, mas não quebra

    def test_nao_recalcula_preco_se_faltar_qualquer_campo_obrigatorio(self):
        campos = ["produto", "largura", "altura", "espessura", "cores_n", "impressao", "milheiros"]
        base = {
            "produto": "Sacola Vazada", "largura": 40, "altura": 50, "espessura": 0.008,
            "cores_n": 3, "impressao": "FRENTE", "milheiros": 30,
        }
        for campo in campos:
            incompleto = {k: v for k, v in base.items() if k != campo}
            with self.subTest(campo_faltando=campo):
                r = app.processar_item_pedido(incompleto)
                self.assertFalse(r["completo"])
                self.assertIsNone(r["preco_preview"])


class TestRecarregarPrecosEmTempoReal(unittest.TestCase):
    """Testa a função por trás do endpoint /admin/recarregar-precos: precisa
    conseguir recarregar sem reiniciar o serviço, e nunca deixar o sistema
    numa situação pior do que estava se o arquivo novo vier ruim."""

    def test_recarga_com_arquivo_valido_funciona(self):
        resultado = app.recarregar_tabela_precos()
        self.assertTrue(resultado["sucesso"])
        self.assertGreaterEqual(resultado["linhas_tabela"], 10)

    def test_recarga_com_arquivo_invalido_preserva_tabela_atual(self):
        caminho_original = app.CAMINHO_CALCULADORA
        tabela_antes = app.TABELA
        try:
            app.CAMINHO_CALCULADORA = "/caminho/que/nao/existe.html"
            resultado = app.recarregar_tabela_precos()
            self.assertFalse(resultado["sucesso"])
            self.assertEqual(app.TABELA, tabela_antes)  # nada mudou, sistema continua saudável
        finally:
            app.CAMINHO_CALCULADORA = caminho_original

    def test_mudanca_na_tabela_e_vista_pelo_modulo_de_ferramentas(self):
        """Prova que a recarga não fica 'presa' no módulo precos - o resto do
        sistema (tools.py) precisa ver o valor novo na próxima chamada."""
        tabela_original = app.TABELA
        try:
            app.TABELA = [{"m": "Virgem BD", "i": "IMPRESSÃO FRENTE", "c": "até 2 cores", "v1": 999.0, "v2": 999.0, "v3": 999.0}]
            resultado = tools_app.executar_calcular_orcamento({
                "produto": "Sacola Vazada", "material": "Virgem BD", "largura": 40, "altura": 50,
                "espessura": 0.008, "cores_n": 1, "impressao": "FRENTE", "milheiros": 30,
            })
            self.assertGreater(resultado.get("preco_por_milheiro", 0), 1000)  # reflete o 999 forçado
        finally:
            app.TABELA = tabela_original


if __name__ == "__main__":
    unittest.main()
