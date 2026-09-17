import os
import unittest
from unittest.mock import patch

os.environ.setdefault('CLAUDE_API_KEY', 'test-key')
os.environ.setdefault('DATABASE_URL', 'postgres://test:test@localhost/test')
os.environ.setdefault('EVOLUTION_API_URL', 'http://test.invalid')
os.environ.setdefault('EVOLUTION_API_KEY', 'test-key')
os.environ.setdefault('PROPRIETARIO_TELEFONE', '5500000000000')
os.environ.setdefault('CONSULTOR_TELEFONE', '5500000000000')
os.environ.setdefault('WEBHOOK_SECRET', 'test-secret')
os.environ.setdefault('ADMIN_WEBHOOK_SECRET', 'test-admin-secret')
os.environ.setdefault('ANTI_BOT_REDIS_URL', 'redis://localhost:6379/15')

from app.cro import _bucket, gerar_hipoteses, criar_experimento, registrar_evento, relatorio


class TestCro(unittest.TestCase):
    def test_bucket_is_deterministic(self):
        self.assertEqual(_bucket('conversation-1', 'experiment-1'), _bucket('conversation-1', 'experiment-1'))
        self.assertGreaterEqual(_bucket('conversation-1', 'experiment-1'), 0)
        self.assertLessEqual(_bucket('conversation-1', 'experiment-1'), 1)

    def test_hypothesis_for_low_quote_rate(self):
        result = gerar_hipoteses({'mensagem_cliente': 100, 'orcamento_apresentado': 10})
        self.assertTrue(any(x['metrica'] == 'orcamento_apresentado' for x in result))

    def test_hypothesis_for_low_close_rate(self):
        result = gerar_hipoteses({'orcamento_apresentado': 100, 'pedido_fechado': 5})
        self.assertTrue(any(x['metrica'] == 'pedido_fechado' for x in result))

    def test_invalid_experiment_never_touches_database(self):
        with patch('app.cro.garantir_estrutura_comercial') as ensure:
            result = criar_experimento('', '', 'pedido_fechado', 50)
        self.assertFalse(result['ok'])
        ensure.assert_not_called()

    def test_experiment_requires_safe_percentage(self):
        with patch('app.cro.garantir_estrutura_comercial') as ensure:
            result = criar_experimento('Teste', 'Hipótese', 'pedido_fechado', 100)
        self.assertFalse(result['ok'])
        ensure.assert_not_called()

    def test_unknown_event_is_rejected(self):
        self.assertFalse(registrar_evento('c', 'u', 'preco_alterado'))

    def test_relatorio_uses_unique_conversations_for_rates(self):
        class FakeCursor:
            def __init__(self):
                self.calls = 0
            def execute(self, *args, **kwargs):
                self.calls += 1
            def fetchall(self):
                if self.calls == 1:
                    return [("mensagem_cliente", 30), ("orcamento_apresentado", 3), ("pedido_fechado", 1)]
                return [("mensagem_cliente", 3), ("orcamento_apresentado", 3), ("pedido_fechado", 1)]
            def close(self):
                pass
        class FakeDb:
            def __init__(self):
                self.cur = FakeCursor()
            def cursor(self):
                return self.cur

        fake_db = FakeDb()
        with patch('app.cro.garantir_estrutura_comercial'), \
             patch('app.cro.get_db', return_value=fake_db), \
             patch('app.cro.release_db'):
            result = relatorio(720)

        self.assertEqual(result['eventos']['mensagem_cliente'], 30)
        self.assertEqual(result['conversas_unicas']['mensagem_cliente'], 3)
        self.assertEqual(result['taxas']['mensagem_para_orcamento'], 100.0)
        self.assertEqual(result['taxas']['orcamento_para_fechamento'], 33.33)

    def test_relatorio_zero_denominator_returns_zero(self):
        class FakeCursor:
            def __init__(self):
                self.calls = 0
            def execute(self, *args, **kwargs):
                self.calls += 1
            def fetchall(self):
                return []
            def close(self):
                pass
        class FakeDb:
            def cursor(self):
                return FakeCursor()

        with patch('app.cro.garantir_estrutura_comercial'), \
             patch('app.cro.get_db', return_value=FakeDb()), \
             patch('app.cro.release_db'):
            result = relatorio(720)

        self.assertEqual(result['taxas']['mensagem_para_orcamento'], 0.0)
        self.assertEqual(result['taxas']['orcamento_para_fechamento'], 0.0)
        self.assertEqual(result['taxas']['transferencia'], 0.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
