Meu agente de WhatsApp está finalizado. Quero garantir que ele NUNCA pare de funcionar ou dê erro em produção. 

Faça uma análise completa de resiliência com:

1. **Revisão de Código Anti-Falhas**:
   - Identifique todos os pontos cegos (timeouts, falhas de API, memória, concorrência).
   - Sugira implementação de retry com backoff exponencial para todas as chamadas externas (API Meta, banco, IA).

2. **Estratégias de Fallback**:
   - O que fazer quando a API do WhatsApp falha?
   - O que fazer quando a IA (LLM) está fora do ar?
   - O que fazer quando o banco de dados cai?

3. **Monitoramento e Alertas**:
   - Quais métricas monitorar em tempo real (latência, taxa de erro, fila de mensagens)?
   - Sugira integração com Sentry/DataDog/Prometheus.

4. **Testes de Estresse e Caos**:
   - Crie um plano de testes para simular: pico de 1000 mensagens/min, perda de conexão, timeout de 30s.
   - Implemente health checks e endpoints de status.

5. **Reconexão Automática**:
   - Lógica de reconnect com backoff para WebSocket/HTTP.
   - Persistência de estado em caso de reinicialização.

6. **Tratamento de Erros Granular**:
   - Categorize erros em: transientes, permanentes, de negócio.
   - Para cada categoria, defina ação: retry, notificar admin, ignorar, ou enfileirar.

7. **Escalabilidade**:
   - Se usar filas (RabbitMQ/SQS), garanta que mensagens não sejam perdidas.
   - Sugira limite de concorrência para não estourar rate limits da Meta.

8. **Logs Estruturados**:
   - Padronize logs com correlation-id para rastrear cada conversa do início ao fim.

Por favor, me entregue:
- Um checklist de implementação prioritária.
- Trechos de código para os principais pontos críticos (retry, fallback, health check).
- Um script de teste de carga simples.
