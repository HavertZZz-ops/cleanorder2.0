from django.apps import AppConfig


class ClearorderConfig(AppConfig):
    name = 'ClearOrder'

    def ready(self):
        # Registra os receivers de post_save/pre_save das notificacoes.
        from . import signals  # noqa: F401
