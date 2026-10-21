# -*- coding: utf-8 -*-
"""
Dados de analytics para o painel do monitor.

Separado das views de proposito: os testes importam daqui e nao precisam
subir servidor nem montar request. Todo retorno e JSON-serializavel, porque
vai para o template via `|json_script` (que escapa o conteudo, evitando
injecao de HTML vindo de dados do banco).
"""
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import SolicitacaoReparo, Vistoria


# Categorias REAIS do formulario de chamado. O pedido original citava
# "Manutencao / Limpeza / Estrutural", que nao existem no projeto.
CATEGORIAS_CHAMADO = [
    ('eletrica', 'Elétrica', '#0b4a42'),
    ('hidraulica', 'Hidráulica', '#00e6a5'),
    ('mobiliario', 'Mobiliário', '#3b82f6'),
    ('outros', 'Outros', '#94a3b8'),
]

# Categoria -> rotulo. Voltamos sempre as quatro, mesmo as que estao zeradas,
# para o grafico nao "sumir" com fatias conforme o banco cresce.
_ROTULOS = {chave: rotulo for chave, rotulo, _ in CATEGORIAS_CHAMADO}
_CORES = {chave: cor for chave, _, cor in CATEGORIAS_CHAMADO}


def _segunda_de(dia):
    """Segunda-feira da semana de `dia` (as semanas comecam na segunda)."""
    return dia - timedelta(days=dia.weekday())


def serie_notas_semanais(dias=30, quarto=None, hoje=None):
    """
    Media de nota por semana nos ultimos `dias` dias.

    `quarto` opcional recorta a serie para um quarto (pagina de detalhe).
    `hoje` permite usar a data simulada da sessao no lugar da data real.
    Devolve sempre uma entrada por semana do intervalo, mesmo nas vazias, com
    media=None. O Chart.js usa null para quebrar a linha em vez de mentir com
    um zero - zero aqui significaria "nota zero", que e outra coisa.
    """
    hoje = hoje or timezone.localdate()
    inicio = hoje - timedelta(days=dias)

    consulta = Vistoria.objects.filter(
        data_vistoria__gte=inicio, data_vistoria__lte=hoje
    )
    if quarto is not None:
        consulta = consulta.filter(quarto=quarto)

    notas = list(consulta.values_list('data_vistoria', 'nota'))

    semanas = {}
    for dia, nota in notas:
        if nota is None:
            continue
        semanas.setdefault(_segunda_de(dia), []).append(nota)

    serie = []
    cursor = _segunda_de(inicio)
    limite = _segunda_de(hoje)
    while cursor <= limite:
        valores = semanas.get(cursor, [])
        serie.append({
            'inicio': cursor.isoformat(),
            'media': round(sum(valores) / len(valores), 2) if valores else None,
            'total': len(valores),
        })
        cursor += timedelta(days=7)
    return serie


def chamados_por_categoria(quarto=None):
    """Chamados agrupados por categoria, sempre com as quatro categorias.

    `quarto` opcional recorta os chamados dos moradores daquele quarto.
    """
    consulta = SolicitacaoReparo.objects.all()
    if quarto is not None:
        consulta = consulta.filter(estudante__quarto=quarto)

    contagem = {
        linha['categoria']: linha['total']
        for linha in consulta
        .values('categoria')
        .annotate(total=Count('id'))
    }
    return [
        {
            'chave': chave,
            'rotulo': rotulo,
            'total': contagem.get(chave, 0),
            'cor': _CORES[chave],
        }
        for chave, rotulo, _ in CATEGORIAS_CHAMADO
    ]


def kpis(dias=30, hoje=None):
    """Numeros grandes do topo do painel."""
    hoje = hoje or timezone.localdate()
    inicio = hoje - timedelta(days=dias)

    do_periodo = Vistoria.objects.filter(data_vistoria__gte=inicio, data_vistoria__lte=hoje)

    avaliadas = list(do_periodo.filter(nota__isnull=False).values_list('nota', flat=True))
    total_vistorias = do_periodo.count()
    aprovadas = do_periodo.filter(aprovado=True).count()

    return {
        'media_nota': round(sum(avaliadas) / len(avaliadas), 1) if avaliadas else None,
        'total_vistorias': total_vistorias,
        'aproveitamento': round(aprovadas * 100 / total_vistorias) if total_vistorias else 0,
        'chamados_abertos': SolicitacaoReparo.objects.exclude(status='Verificado').count(),
        'chamados_total': SolicitacaoReparo.objects.count(),
        'periodo_dias': dias,
    }


def payload_analytics(dias=30, hoje=None):
    """Tudo junto - o que vai para o contexto e para o JSON."""
    return {
        'serie_notas': serie_notas_semanais(dias, hoje=hoje),
        'categorias': chamados_por_categoria(),
        'kpis': kpis(dias, hoje=hoje),
    }
