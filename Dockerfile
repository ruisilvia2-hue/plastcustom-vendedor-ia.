FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt --no-cache-dir
COPY app/ ./app/
COPY Plastcustom_Orcamento.html .
EXPOSE 3000
# Servidor de produção (Gunicorn) no lugar do servidor de desenvolvimento do Flask.
# --workers: quantos processos atendem requisições em paralelo (ajustável via variável WEB_CONCURRENCY)
# --timeout: tempo máximo por requisição (aumentado pois o robô faz chamadas de IA + banco por mensagem)
# NÃO usamos --preload de propósito: cada worker precisa criar seu próprio pool de conexões
# com o banco depois de nascer, não compartilhar um pool criado antes de existir.
# "app:app" continua funcionando igual a antes: importa o PACOTE app/ (por causa do
# __init__.py) e usa a variável "app" (o Flask) definida lá dentro.
CMD ["sh", "-c", "gunicorn --workers ${WEB_CONCURRENCY:-4} --timeout 60 --bind 0.0.0.0:${PORT:-3000} app:app"]
