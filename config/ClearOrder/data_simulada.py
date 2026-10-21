# -*- coding: utf-8 -*-
"""Data de referencia da sessao para o simulador do Demo Day.

A data simulada fica na sessao (``data_simulada``), nunca no banco. Assim
cada usuario pode avancar o proprio calendario sem alterar a data real dos
outros. A leitura e feita sob demanda (lazy): a cada request as views que
dependem de data chamam ``data_referencia`` e recalculam a semana, a
vistoria e o cronograma com o valor atual da sessao.
"""

from datetime import date, timedelta

from django.utils import timezone

SESSION_KEY = 'data_simulada'


def _como_data(valor):
    """Converte o valor persistido na sessao em ``date``, ou None."""
    if isinstance(valor, date):
        return valor
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor))
    except (TypeError, ValueError):
        return None


def data_referencia(request=None):
    """Data simulada da sessao; cai para a data local real se ausente/invalida."""
    sessao = getattr(request, 'session', None) if request is not None else None
    if sessao is not None:
        simulada = _como_data(sessao.get(SESSION_KEY))
        if simulada is not None:
            return simulada
    return timezone.localdate()


def avancar_dia(request):
    """Avanca a data da sessao em um dia e devolve a nova data."""
    nova_data = data_referencia(request) + timedelta(days=1)
    # ISO string e serializavel pelo backend JSON de sessoes do Django.
    request.session[SESSION_KEY] = nova_data.isoformat()
    return nova_data


def limpar_simulacao(request):
    """Volta a usar a data real (util para testes e para um futuro reset)."""
    sessao = getattr(request, 'session', None)
    if sessao is not None:
        sessao.pop(SESSION_KEY, None)
