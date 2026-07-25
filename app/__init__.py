"""
Ponto de entrada do pacote: cria a aplicação Flask e registra as rotas.

O Gunicorn aponta para "app:app" (veja o Dockerfile) - ou seja, importa este pacote
e usa a variável "app" definida aqui embaixo. Isso continua funcionando exatamente
igual a antes, mesmo com o código agora dividido em vários arquivos.
"""
from flask import Flask

from app.config import limiter


def create_app():
    flask_app = Flask(__name__)
    limiter.init_app(flask_app)

    # Importa o Blueprint aqui dentro (não lá no topo do arquivo) para evitar
    # "importação circular": webhook.py importa de outros módulos do pacote, que
    # só existem depois que este __init__.py começa a rodar.
    from app.webhook import bp
    flask_app.register_blueprint(bp)

    return flask_app


app = create_app()
