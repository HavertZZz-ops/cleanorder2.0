# -*- coding: utf-8 -*-
"""
Ranking gamificado dos alojamentos — "Quarto Ouro".

A pontua��ão combina duas coisas que o sistema ja mede:

    pontuacao = media_das_notas + bonus_de_streak

O bonus de streak vale 0.3 por dia ate um teto de 10 dias (+3.0 no maximo).
Ou seja: a media manda, mas uma sequencia longa de aprovacoesconsegue
disputar a primeira colocacao. O teto evita que um quarto com streak de 90
dias enterraria um quarto efetivamente bem avaliado.

Regra deliberada: quarto SEM nenhuma vistoria avaliada nao entra no ranking.
Senao um quarto novo, vazio, "ganharia" por ausencia de dados.

Desempate: media -> streak -> numero do quarto (deterministico, para o
ranking nao mudar de ordem a cada reload).
"""
from .relatorios import PERIODOS, data_limite, quartos_com_moradores, montar_relatorios
from .models import Vistoria

PESO_STREAK = 0.3
TETO_STREAK = 10

MEDALHAS = {
    1: {'medalha': '🥇', 'rotulo': 'Quarto Ouro', 'cor': 'emerald'},
    2: {'medalha': '🥈', 'rotulo': 'Quarto Prata', 'cor': 'slate'},
    3: {'medalha': '🥉', 'rotulo': 'Quarto Bronze', 'cor': 'amber'},
}

TOTAL_PODIO = 3


def calcular_pontuacao(media, streak):
    """Pontuacao de um quarto. Media None (sem nota) vale 0."""
    base = media if media is not None else 0.0
    bonus = min(streak or 0, TETO_STREAK) * PESO_STREAK
    return round(base + bonus, 2)


def ordenar(relatorios):
    """Ordena por pontuacao, media, streak e numero - sempre deterministico."""
    return sorted(
        relatorios,
        key=lambda r: (
            -calcular_pontuacao(r['media'], r['streak']),
            -(r['media'] if r['media'] is not None else -1),
            -(r['streak'] or 0),
            str(r['quarto']),
        ),
    )


def ranking_quartos(periodo='tudo', quarto_filtrado=None, total=None, hoje=None):
    """
    Lista ordenada com posicao, medalha e pontuacao.

    `total` limita quantos itens saem (o podio pede 3). `hoje` permite que
    o recorte de periodo use a data simulada da sessao.
    """
    if quarto_filtrado is None:
        quartos = quartos_com_moradores()
    else:
        quartos = quartos_com_moradores(quarto_filtrado)

    vistorias = Vistoria.objects.select_related('quarto')
    limite = data_limite(periodo, hoje=hoje)
    if limite is not None:
        vistorias = vistorias.filter(data_vistoria__gte=limite)
    if quarto_filtrado is not None:
        vistorias = vistorias.filter(quarto_id=quarto_filtrado)

    relatorios = montar_relatorios(quartos, vistorias)

    # sem vistoria avaliada nao disputa posto
    elegiveis = [r for r in relatorios if r['total_vistorias'] > 0]

    saida = []
    for posicao, linha in enumerate(ordenar(elegiveis), start=1):
        faixa = MEDALHAS.get(posicao, {'medalha': str(posicao), 'rotulo': '', 'cor': 'slate'})
        saida.append({
            'posicao': posicao,
            'quarto': linha['quarto'],
            'media': linha['media'],
            'streak': linha['streak'] or 0,
            'total_vistorias': linha['total_vistorias'],
            'aprovacoes': linha['aprovacoes'],
            'reprovacoes': linha['reprovacoes'],
            'ponto': calcular_pontuacao(linha['media'], linha['streak']),
            'medalha': faixa['medalha'],
            'rotulo': faixa['rotulo'],
            'cor': faixa['cor'],
        })
        if total and posicao >= total:
            break

    return saida


def podio(periodo='tudo', hoje=None):
    """Top 3 - o que o card do monitor mostra."""
    return ranking_quartos(periodo=periodo, total=TOTAL_PODIO, hoje=hoje)


def posicao_do_quarto(quarto, periodo='tudo', hoje=None):
    """
    Posicao de UM quarto no ranking completo.

    Devolve None quando o quarto nao disputa (sem vistoria avaliada) - o
    aluno entao ve o card de NOTA, nunca um "Quarto Ouro" vazio de sentido.
    """
    if quarto is None:
        return None

    for linha in ranking_quartos(periodo=periodo, hoje=hoje):
        if linha['quarto'].pk == quarto.pk:
            return linha
    return None


def eh_quarto_ouro(quarto, periodo='tudo'):
    return (posicao_do_quarto(quarto, periodo) or {}).get('posicao') == 1