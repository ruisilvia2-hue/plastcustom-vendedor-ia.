"""
Teste de carga do /webhook, em escala REALISTA para um negócio como a Plastcustom
(um vendedor, um número de WhatsApp) - não simula 1000 mensagens/minuto porque
isso não é uma meta que faz sentido pro seu volume real de clientes.

O que este script simula: várias conversas "ao mesmo tempo" (concorrência real que
pode acontecer em horário de pico, tipo várias pessoas te mandando mensagem na
mesma hora), medindo tempo de resposta e taxa de erro.

COMO RODAR:
    pip install requests --break-system-packages   # se ainda não tiver
    python3 teste_carga.py --url https://SEU-DOMINIO --secret SEU_WEBHOOK_SECRET

Parâmetros ajustáveis (veja mais abaixo, em CONFIGURAÇÃO):
    CONVERSAS_SIMULTANEAS: quantos "clientes" mandam mensagem ao mesmo tempo
    MENSAGENS_POR_CONVERSA: quantas mensagens cada um manda, em sequência

⚠️ CUIDADO: isso manda requisições de verdade pro seu /webhook, que por sua vez
chama a API da Claude de verdade (gasta créditos!) e pode disparar notificações
reais pro seu WhatsApp de dono/consultor. Recomendado rodar isso poucas vezes,
não como rotina, e de preferência fora do horário comercial.
"""
import argparse
import concurrent.futures
import statistics
import time
import uuid

import requests

# ============================================================
# CONFIGURAÇÃO — ajuste aqui conforme o que fizer sentido testar
# ============================================================
CONVERSAS_SIMULTANEAS = 15   # clientes diferentes "ao mesmo tempo" - realista pra um pico de horário comercial
MENSAGENS_POR_CONVERSA = 3   # mensagens em sequência por cliente
MENSAGEM_DE_TESTE = "Oi, quero um orçamento de sacolas personalizadas"


def enviar_mensagem(url, secret, telefone, mensagem):
    inicio = time.monotonic()
    try:
        r = requests.post(
            f"{url}/webhook",
            json={"telefone": telefone, "mensagem": mensagem, "mensagem_id": str(uuid.uuid4())},
            headers={"X-Webhook-Secret": secret},
            timeout=60,
        )
        duracao = time.monotonic() - inicio
        return {"status": r.status_code, "duracao": duracao, "erro": None}
    except requests.exceptions.RequestException as e:
        duracao = time.monotonic() - inicio
        return {"status": None, "duracao": duracao, "erro": str(e)}


def simular_uma_conversa(url, secret, indice):
    telefone_fake = f"55419900{indice:05d}"
    resultados = []
    for _ in range(MENSAGENS_POR_CONVERSA):
        resultados.append(enviar_mensagem(url, secret, telefone_fake, MENSAGEM_DE_TESTE))
    return resultados


def main():
    parser = argparse.ArgumentParser(description="Teste de carga realista do /webhook")
    parser.add_argument("--url", required=True, help="URL base do serviço (ex: https://seller.seudominio.com)")
    parser.add_argument("--secret", required=True, help="Valor do WEBHOOK_SECRET")
    args = parser.parse_args()

    print(f"Simulando {CONVERSAS_SIMULTANEAS} conversas ao mesmo tempo, "
          f"{MENSAGENS_POR_CONVERSA} mensagens cada ({CONVERSAS_SIMULTANEAS * MENSAGENS_POR_CONVERSA} requisições no total)...")
    print("⚠️  Isso gasta créditos reais de IA e pode notificar seu WhatsApp de verdade.\n")

    inicio_total = time.monotonic()
    todos_resultados = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONVERSAS_SIMULTANEAS) as executor:
        futuros = [
            executor.submit(simular_uma_conversa, args.url, args.secret, i)
            for i in range(CONVERSAS_SIMULTANEAS)
        ]
        for futuro in concurrent.futures.as_completed(futuros):
            todos_resultados.extend(futuro.result())
    duracao_total = time.monotonic() - inicio_total

    sucessos = [r for r in todos_resultados if r["status"] == 200]
    falhas = [r for r in todos_resultados if r["status"] != 200]
    duracoes = [r["duracao"] for r in sucessos]

    print("=" * 50)
    print("RESULTADO")
    print("=" * 50)
    print(f"Total de requisições: {len(todos_resultados)}")
    print(f"Sucesso (200):        {len(sucessos)}")
    print(f"Falha:                {len(falhas)}")
    print(f"Tempo total do teste: {duracao_total:.1f}s")
    if duracoes:
        print(f"\nTempo de resposta (segundos):")
        print(f"  Mínimo:   {min(duracoes):.2f}s")
        print(f"  Média:    {statistics.mean(duracoes):.2f}s")
        print(f"  Mediana:  {statistics.median(duracoes):.2f}s")
        print(f"  Máximo:   {max(duracoes):.2f}s")
    if falhas:
        print(f"\nDetalhe das falhas (até 5 primeiras):")
        for f in falhas[:5]:
            print(f"  status={f['status']} erro={f['erro']}")
    print("\nLembrete: alguns segundos de tempo de resposta são ESPERADOS aqui -")
    print("o webhook tem uma pausa proposital de 3s (proteção contra mensagens em")
    print("sequência) + o tempo real da IA processando. Isso não é 'lentidão ruim'.")


if __name__ == "__main__":
    main()
