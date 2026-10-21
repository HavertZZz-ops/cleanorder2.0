# -*- coding: utf-8 -*-
"""
Exportacao do "Relatorio Oficial de Desempenho dos Alojamentos".

CSV  -> planilha para Excel / LibreOffice
PDF  -> pagina de impressao (o proprio navegador gera o PDF em "Salvar como PDF")

Decisao: nenhum pacote de PDF. WeasyPrint/xhtml2pdf tem dependencias pesadas
e quebram em Windows com frequencia; o caminho nativo do navegador imprime
com as cores institucionais Certain e sem mais uma dependencia no venv.
"""
import csv
import io as _io

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import render
from datetime import timedelta

from django.utils import timezone

from .data_simulada import data_referencia
from .models import Estudante, Quarto, SolicitacaoReparo, Vistoria
from .cronograma_utils import janela_da_semana

NOME_ARQUIVO_CSV = 'relatorio_alojamentos_cleanorder.csv'

# Excel pt-BR usa PONTO como separador decimal. Com virgula, o Excel abre o
# arquivo inteiro em UMA coluna. Por isso o delimitador e ';'.
DELIMITADOR_CSV = ';'

# Cabecalho exigindo UTF-8 com BOM: sem ele o Excel do Windows mostra
# "ElÃ©trica". O Content-Type continua utf-8, como especificado.
BOM_UTF8 = '\ufeff'

CABECALHOS_CSV = [
    'Quarto',
    'Moradores',
    'Media de Notas',
    'Quantidade de Vistorias',
    'Aprovacoes',
    'Reprovacoes',
    'Streak Atual',
    'Ultima Vistoria',
]

PERIODOS = {
    'tudo': None,
    '4semanas': 28,
    'semana': None,   # resolvido em data_limite()
}


def data_limite(chave, hoje=None):
    """Data inicial do periodo, ou None para 'tudo'."""
    data_ref = hoje or timezone.localdate()
    if chave == 'semana':
        return janela_da_semana(offset=0, hoje=data_ref)[0]
    dias = PERIODOS.get(chave)
    return data_ref - timedelta(days=dias) if dias else None


def montar_relatorios(quartos_com_moradores, vistorias):
    """
    Resumo por quarto. Mesma funcao usada na tela de Gestao, no CSV e no
    relatorio impresso - assim os tres nunca discordam sobre os numeros.
    """
    relatorios = []
    for item in quartos_com_moradores:
        q = item['quarto']
        do_quarto = vistorias.filter(quarto=q)

        notas = list(do_quarto.filter(nota__isnull=False).values_list('nota', flat=True))
        media = round(sum(notas) / len(notas), 1) if notas else None

        relatorios.append({
            'quarto': q,
            'moradores': item['moradores'],
            'media': media,
            'total_vistorias': len(notas),
            'aprovacoes': do_quarto.filter(aprovado=True).count(),
            'reprovacoes': do_quarto.filter(aprovado=False).count(),
            'streak': q.streak_dias,
            'status': q.status,
            'ultima_vistoria': do_quarto.order_by('-data_vistoria', '-id').first(),
        })
    return relatorios


def quartos_para_painel():
    """
    Um item por QUARTO para a sidebar do painel de vistorias.

    Antes o painel iterava ESTUDANTES e desenhava o quarto de cada um: dois
    colegas no 104 renderizavam o mesmo card duas vezes. `.distinct()` no
    queryset de Estudante nao resolvia - sao dois estudantes diferentes, com
    pks diferentes. Aqui a unidade da lista e o quarto.

    O filter por `estudante__isnull=False` traz so quartos ocupados; as
    anotacoes contam moradores e vistorias com `distinct=True` para o GROUP
    BY nao multiplicar linhas.
    """
    from django.db.models import Count

    return (
        Quarto.objects
        .filter(estudante__isnull=False)
        .annotate(
            moradores_count=Count('estudante', distinct=True),
            vistorias_count=Count('vistoria', distinct=True),
        )
        .distinct()
        .order_by('bloco', 'numero')
    )


# Status que representam um chamado ainda aberto. O projeto grava
# 'Em Aberto'; 'EM_ABERTO' fica aceito para bases/importacoes que usem o
# codigo do enum em vez do rotulo.
STATUS_CHAMADO_ABERTO = ('Em Aberto', 'EM_ABERTO')


def metricas_quarto(quarto):
    """
    KPIs do quarto selecionado no painel de vistorias.

    Diferente de `kpis()` (que olha os ultimos 30 dias de TODOS os quartos),
    aqui o recorte e o quarto e o historico inteiro: e o que o monitor
    espera ao abrir /quarto/<id>/. As quatro chaves sao sempre numericas -
    nunca None - para o template nao renderizar "—", "%" solto ou vazio.

    - media_nota: media das vistorias COM nota; 0.0 quando nao ha nenhuma.
    - aproveitamento: aprovadas / total de vistorias do quarto (0 se total=0).
    - total_vistorias: todas as vistorias do quarto, inclusive sem nota.
    - chamados_abertos: chamados dos moradores do quarto ainda abertos.
    """
    vistorias = Vistoria.objects.filter(quarto=quarto)
    notas = list(
        vistorias.filter(nota__isnull=False).values_list('nota', flat=True)
    )
    total_vistorias = vistorias.count()
    aprovadas = vistorias.filter(aprovado=True).count()

    chamados = SolicitacaoReparo.objects.filter(estudante__quarto=quarto)
    chamados_abertos = chamados.filter(status__in=STATUS_CHAMADO_ABERTO).count()

    return {
        'media_nota': round(sum(notas) / len(notas), 1) if notas else 0.0,
        'aproveitamento': (
            round(aprovadas * 100 / total_vistorias) if total_vistorias else 0
        ),
        'total_vistorias': total_vistorias,
        'chamados_abertos': chamados_abertos,
        'chamados_total': chamados.count(),
    }


def unificar_quartos_duplicados(mostrar=False):
    """
    Funde quartos com o mesmo (numero, bloco) mantendo o de menor id.

    Depois da migracao 0008 o banco nao aceita mais duplicatas; isto serve
    para Bases que ja tinham o problema antes da restricao. Nao apaga nada
    silenciosamente: devolve um relatorio do que foi movido.
    """
    from ClearOrder.models import Cronograma, Estudante, Vistoria

    grupos = {}
    for quarto in Quarto.objects.order_by('id'):
        grupos.setdefault((quarto.numero, quarto.bloco), []).append(quarto)

    relatorio = []
    for (numero, bloco), lista in sorted(grupos.items()):
        if len(lista) < 2:
            continue
        keeper = lista[0]           # menor id
        absorvidos = lista[1:]

        for excedente in absorvidos:
            Estudante.objects.filter(quarto=excedente).update(quarto=keeper)
            Vistoria.objects.filter(quarto=excedente).update(quarto=keeper)
            # Cronograma basta: as Tarefas apontam para ele por FK e acompanham
            # automaticamente. Tentar `update()` atraves de join
            # (cronograma__quarto) é proibido pelo Django.
            Cronograma.objects.filter(quarto=excedente).update(quarto=keeper)

            # streak e status: fica o melhor resultado, nunca o pior
            keeper.streak_dias = max(keeper.streak_dias or 0, excedente.streak_dias or 0)
            if keeper.status == Quarto.STATUS_PENDENTE:
                keeper.status = excedente.status
            keeper.save()

            excedente.delete()
            relatorio.append((numero, bloco, excedente.pk))

    if mostrar and relatorio:
        for numero, bloco, pk in relatorio:
            print('  Quarto %s - Bloco %s: duplicado #%d removido' % (numero, bloco, pk))
    return relatorio


def _quarto_id_valido(bruto):
    if bruto in (None, ''):
        return None
    try:
        return int(bruto)
    except (TypeError, ValueError):
        return None


def quartos_com_moradores(quarto_id=None):
    """
    Lista de {'quarto': Quarto, 'moradores': qs}.

    Com quarto_id, devolve SÓ esse quarto - é o filtro do relatório.
    """
    qs = Quarto.objects.all().order_by('bloco', 'numero')
    if quarto_id is not None:
        qs = qs.filter(pk=quarto_id)

    return [
        {
            'quarto': q,
            'moradores': Estudante.objects.filter(quarto=q).select_related('user'),
        }
        for q in qs
    ]


def coletar_dados(request):
    """Monta (relatorios, contexto) respeitando ?periodo= e ?quarto=."""
    data_ref = data_referencia(request)
    periodo = request.GET.get('periodo', 'tudo')
    if periodo not in PERIODOS:
        periodo = 'tudo'

    quarto_filtrado = _quarto_id_valido(request.GET.get('quarto'))

    # O filtro restringe a TABELA do relatório, não os <select> da tela:
    # os dropdowns de vincular/desvincular precisam continuar vendo todos.
    quartos_do_relatorio = quartos_com_moradores(quarto_filtrado)

    vistorias = Vistoria.objects.select_related('quarto')
    limite = data_limite(periodo, hoje=data_ref)
    if limite is not None:
        vistorias = vistorias.filter(data_vistoria__gte=limite)
    if quarto_filtrado is not None:
        vistorias = vistorias.filter(quarto_id=quarto_filtrado)

    relatorios = montar_relatorios(quartos_do_relatorio, vistorias)

    contexto = {
        'relatorios': relatorios,
        'periodo_escolhido': periodo,
        'quarto_filtrado': quarto_filtrado or '',
        'total_quartos': Quarto.objects.count(),
        'total_vistorias_periodo': vistorias.count(),
        'aprovacoes_periodo': vistorias.filter(aprovado=True).count(),
    }
    return relatorios, contexto


def _negado(request):
    """403 para quem nao e monitor. Mesmo tratamento da tela de Gestao."""
    return HttpResponseForbidden(
        'Acesso restrito: apenas monitores podem exportar relatorios.'
    )


def _sem_monitor(request):
    return hasattr(request.user, 'monitor')


@login_required
def exportar_relatorio_csv(request):
    """Planilha do relatorio oficial. Responde sempre com arquivo, nunca tela."""
    if not _sem_monitor(request):
        return _negado(request)

    relatorios, ctx = coletar_dados(request)

    buffer = _io.StringIO()
    escritor = csv.writer(buffer, delimiter=DELIMITADOR_CSV, lineterminator='\r\n')
    escritor.writerow(CABECALHOS_CSV)

    for linha in relatorios:
        q = linha['quarto']
        moradores = ', '.join(m.user.get_full_name() or m.user.username
                              for m in linha['moradores'])
        ultima = linha['ultima_vistoria']
        escritor.writerow([
            str(q),
            moradores or 'sem moradores',
            linha['media'] if linha['media'] is not None else '',
            linha['total_vistorias'],
            linha['aprovacoes'],
            linha['reprovacoes'],
            linha['streak'] or 0,
            ultima.data_vistoria.strftime('%d/%m/%Y') if ultima else 'nunca',
        ])

    resposta = HttpResponse(BOM_UTF8 + buffer.getvalue(), content_type='text/csv; charset=utf-8')
    resposta['Content-Disposition'] = 'attachment; filename="%s"' % NOME_ARQUIVO_CSV
    return resposta


@login_required
def relatorio_pdf_view(request):
    """
    Pagina de impressao do relatorio oficial.

    O PDF sai pelo proprio navegador (imprimir -> Salvar como PDF). O
    `?auto=1` dispara a impressao sozinho; sem ele, o usuario ve a pagina
    primeiro e clica no botao.
    """
    if not _sem_monitor(request):
        return _negado(request)

    relatorios, ctx = coletar_dados(request)

    emissor = request.user.monitor
    nome_emissor = request.user.get_full_name() or request.user.username

    ctx.update({
        'emissor': emissor,
        'nome_emissor': nome_emissor,
        'emitido_em': timezone.now(),
        'imprimir_automatico': request.GET.get('auto') == '1',
        'total_aprovacoes': sum(r['aprovacoes'] for r in relatorios),
        'total_reprovacoes': sum(r['reprovacoes'] for r in relatorios),
        'quartos_aprovados': sum(1 for r in relatorios if r['status'] == 'Aprovado'),
        'media_geral': _media_geral(relatorios),
    })
    return render(request, 'relatorio_impressao.html', ctx)


def _media_geral(relatorios):
    """Media de todas as notas avaliadas (nao a media das medias)."""
    notas = [r['media'] for r in relatorios if r['media'] is not None]
    return round(sum(notas) / len(notas), 1) if notas else None
