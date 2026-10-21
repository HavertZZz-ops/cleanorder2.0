from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import timedelta
import calendar

from .cronograma_utils import janela_da_semana
from .data_simulada import avancar_dia, data_referencia
from .analytics_utils import (
    chamados_por_categoria,
    payload_analytics,
    serie_notas_semanais,
)
from .qrcode_pure import para_svg
from .gamificacao import podio, posicao_do_quarto, eh_quarto_ouro
from .relatorios import (
    quartos_para_painel,
    montar_relatorios,
    quartos_com_moradores,
    exportar_relatorio_csv,
    relatorio_pdf_view,
    metricas_quarto,
    _quarto_id_valido,
)
from . import relatorios as relatorios_mod
from .models import Estudante, Monitor, Tarefa, Quarto, Vistoria, SolicitacaoReparo, Notificacao

# Pesos de cada critério da vistoria. Somam 10 (escala de 0 a 10).
# Nomes e pesos espelham exatamente os checkboxes do painel do monitor.
# Origem: entrevista de validação com o monitor do alojamento.
CRITERIOS_VISTORIA = {
    'chao_lavanderia': 4,
    'organizacao_geral': 3,
    'janelas_livres': 3,
}


def login_view(request):
    if request.user.is_authenticated:
        if hasattr(request.user, 'estudante'):
            return redirect('painel_estudante')
        elif hasattr(request.user, 'monitor'):
            return redirect('painel_monitor')
        else:
            return redirect('/admin/')

    if request.method == 'POST':
        cpf_digitado = request.POST.get('cpf')
        senha_digitada = request.POST.get('password')
        username_para_login = cpf_digitado

        try:
            estudante_encontrado = Estudante.objects.get(cpf=cpf_digitado)
            username_para_login = estudante_encontrado.user.username
        except Estudante.DoesNotExist:
            try:
                monitor_encontrado = Monitor.objects.get(cpf=cpf_digitado)
                username_para_login = monitor_encontrado.user.username
            except Monitor.DoesNotExist:
                pass
                
        usuario = authenticate(request, username=username_para_login, password=senha_digitada)

        if usuario is not None:
            login(request, usuario)
            if hasattr(usuario, 'estudante'):
                return redirect('painel_estudante')
            elif hasattr(usuario, 'monitor'):
                return redirect('painel_monitor')
            else:
                return redirect('/admin/')
        else:
            messages.error(request, 'CPF ou senha inválidos. Tente novamente.')
            return redirect('login')

    return render(request, 'login.html')

def fazer_logout(request):
    logout(request)
    messages.success(request, 'Logout realizado com sucesso.')
    return redirect('login')

def dias_rotulos():
    return ('Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom')


def calendario_semana(quarto, inicio=None, fim=None, hoje=None):
    """
    Monta os 7 dias (segunda -> domingo) da semana atual com o status de vistoria.

    Cada dia vira um dict com:
        data       -> date
        rotulo     -> 'Seg' ... 'Dom'
        status     -> 'aprovado' | 'reprovado' | 'pendente'
        dia_atual  -> bool (para destacar hoje)
        futuro     -> bool (dia ainda por vir; visualmente segue 'pendente')
        vistoria   -> a Vistoria daquele dia, ou None

    Nota: o campo da model e `data_vistoria` (nao `data`).
    Quando ha mais de uma vistoria no mesmo dia, vale a ultima.

    `hoje` permite que o calendario acompanhe a data simulada da sessao.
    """
    data_ref = hoje or timezone.localdate()
    if inicio is None or fim is None:
        inicio, fim = janela_da_semana(offset=0, hoje=data_ref)

    rotulos = dias_rotulos()

    # Se o aluno nao tem quarto, nao ha historico: todos os dias ficam pendentes.
    por_dia = {}
    if quarto is not None:
        vistorias = Vistoria.objects.filter(
            quarto=quarto,
            data_vistoria__gte=inicio,
            data_vistoria__lte=fim,
        ).order_by('data_vistoria', 'id')
        for vistoria in vistorias:
            por_dia[vistoria.data_vistoria] = vistoria

    dias = []
    for i in range(7):
        data = inicio + timedelta(days=i)
        vistoria = por_dia.get(data)
        if vistoria is None:
            status = 'pendente'
        elif vistoria.aprovado:
            status = 'aprovado'
        else:
            status = 'reprovado'

        dias.append({
            'data': data,
            'rotulo': rotulos[data.weekday()],
            'status': status,
            'dia_atual': data == data_ref,
            'futuro': data > data_ref,
            'vistoria': vistoria,
        })

    return dias


@login_required
def painel_estudante(request):
    if hasattr(request.user, 'estudante'):
        estudante = request.user.estudante

        if request.method not in ('GET', 'HEAD', 'POST'):
            return HttpResponseForbidden('Metodo de requisicao nao permitido.')

        if request.method == 'POST':
            if request.POST.get('acao') == 'configurar_quarto':
                return _configurar_quarto_do_estudante(request, estudante)
            messages.warning(request, 'Acao desconhecida.')
            return redirect('painel_estudante')

        # Só as tarefas do QUARTO do aluno na SEMANA ATUAL (segunda->domingo).
        #
        # Filtro por SOBREPOSICAO de intervalos, e nao por `prazo__range`:
        #  - Cronograma nao tem campo `prazo` (ele fica em Tarefa.data_prazo),
        #    entao a consulta literal da spec levantaria FieldError;
        #  - cronogramas criados a mao nao comecam necessariamente na segunda
        #    (existem no banco os de 04/10 a 10/10). Um filtro por faixa exata
        #    perderia esses registos; o de sobreposicao encontra.
        data_ref = data_referencia(request)
        inicio_semana, fim_semana = janela_da_semana(offset=0, hoje=data_ref)

        tarefas = Tarefa.objects.filter(
            Q(estudante_responsavel=estudante)
            | Q(estudante_responsavel_2=estudante)
            | Q(estudante_responsavel__isnull=True),
            cronograma__quarto=estudante.quarto,
            cronograma__ativo=True,
            cronograma__data_inicio__lte=fim_semana,
            cronograma__data_fim__gte=inicio_semana,
        ).distinct().order_by('data_prazo')

        contexto = {
            'estudante': estudante,
            'tarefas': tarefas,
            'semana_inicio': inicio_semana,
            'semana_fim': fim_semana,
            'dias_semana': calendario_semana(estudante.quarto, hoje=data_ref),
            # A autonomia do quarto tambem aparece no painel do aluno: os
            # selects usam apenas moradores do proprio quarto.
            'moradores_quarto': (
                Estudante.objects.filter(quarto=estudante.quarto)
                .select_related('user')
                .order_by('matricula')
                if estudante.quarto else Estudante.objects.none()
            ),
            'modos_divisao': Quarto.MODOS_DIVISAO,
            # Gamificação: posição do quarto do aluno (None = não disputa).
            'meu_podio': posicao_do_quarto(estudante.quarto, hoje=data_ref),
            'eh_quarto_ouro': eh_quarto_ouro(estudante.quarto),
        }
        return render(request, 'painel_estudante.html', contexto)
    elif hasattr(request.user, 'monitor'):
        return redirect('painel_monitor')
    else:
        logout(request)
        messages.error(request, 'Acesso negado: Sua conta não possui um perfil válido. Procure a administração.')
        return redirect('login')
@login_required
def concluir_tarefa(request, tarefa_id):
    if not hasattr(request.user, 'estudante'):
        messages.error(request, 'Apenas estudantes podem concluir tarefas.')
        return redirect('login')

   
    estudante = request.user.estudante

    # Pode concluir quem foi designado (SOLO/DUPLA) ou, no modo LIVRE, qualquer
    # morador do quarto dono do cronograma. O filtro por quarto impede que um
    # aluno assuma tarefa aberta de outro alojamento.
    tarefa = get_object_or_404(
        Tarefa,
        Q(estudante_responsavel=estudante)
        | Q(estudante_responsavel_2=estudante)
        | Q(estudante_responsavel__isnull=True),
        id=tarefa_id,
        cronograma__quarto=estudante.quarto,
    )

    # LIVRE: a primeira conclusao "assume" a tarefa, para que o painel passe a
    # mostrar quem a fez em vez de deixa-la aberta para sempre.
    if tarefa.estudante_responsavel_id is None:
        tarefa.estudante_responsavel = estudante

    if tarefa.status == 'Concluída':
        tarefa.status = 'Pendente'
    else:
        tarefa.status = 'Concluída'

    tarefa.save(update_fields=['estudante_responsavel', 'status'])

    messages.success(request, 'Status da tarefa atualizado com sucesso!')
    return redirect('painel_estudante')
@login_required
def marcar_notificacao_lida(request, notificacao_id):
    """Marca UMA notificacao como lida. Só POST e só a do próprio usuário."""
    if request.method != 'POST':
        return redirect(request.META.get('HTTP_REFERER') or 'painel_estudante')

    notificacao = get_object_or_404(
        Notificacao,
        id=notificacao_id,
        # o filtro por usuario impede marcar a notificacao de outra pessoa
        estudante_destinatario__user=request.user,
    )
    if not notificacao.lida:
        notificacao.lida = True
        notificacao.save(update_fields=['lida'])

    return redirect(request.META.get('HTTP_REFERER') or 'painel_estudante')


@login_required
def limpar_notificacoes(request):
    """Marca TODAS as notificações do usuário como lidas. Só POST."""
    if request.method != 'POST':
        return redirect(request.META.get('HTTP_REFERER') or 'painel_estudante')

    qs = Notificacao.objects.filter(
        lida=False, estudante_destinatario__user=request.user
    )
    total = qs.count()
    if total:
        qs.update(lida=True)
        messages.success(request, '%d notificação(ões) marcada(s) como lida(s).' % total)

    return redirect(request.META.get('HTTP_REFERER') or 'painel_estudante')


@login_required
def gestao_monitor_view(request):
    """
    Painel de gestao do alojamento (substitui o /admin para o monitor).

    GET  -> formularios de cadastro/vinculo + relatorios por quarto
    POST -> acao em `acao`: 'novo_estudante' | 'vincular_quarto' |
            'configurar_quarto' | 'condecorar_representante'

    Acesso de monitor ou admin (staff): quem nao tem esse perfil recebe 403.
    """
    if not (hasattr(request.user, 'monitor') or request.user.is_staff):
        return HttpResponseForbidden(
            'Acesso restrito: apenas monitores ou administradores podem acessar a gestao do alojamento.'
        )

    if request.method not in ('GET', 'HEAD', 'POST'):
        return HttpResponseForbidden('Metodo de requisicao nao permitido.')

    if request.method == 'POST':
        acao = request.POST.get('acao')

        if acao == 'novo_estudante':
            return _criar_estudante(request)

        if acao == 'vincular_quarto':
            return _vincular_estudante_quarto(request)

        if acao == 'configurar_quarto':
            return _configurar_quarto(request)

        if acao == 'condecorar_representante':
            return _condecorar_representante(request)

        messages.warning(request, 'Acao desconhecida.')

    # ---------- GET: filtros ----------
    data_ref = data_referencia(request)
    periodos = {
        'tudo': None,
        '4semanas': data_ref - timedelta(days=28),
        'semana': janela_da_semana(offset=0, hoje=data_ref)[0],
    }
    periodo_escolhido = request.GET.get('periodo', 'tudo')
    if periodo_escolhido not in periodos:
        periodo_escolhido = 'tudo'

    # ---------- locais ----------
    # Lista completa: alimenta os <select> de vincular/desvincular, que
    # precisam ver todos os quartos mesmo com o relatório filtrado.
    quartos_com_moradores = relatorios_mod.quartos_com_moradores()

    # ---------- relatorios ----------
    # int para filtrar; '' no contexto quando não há filtro (mesma forma que
    # coletar_dados usa, para tela e exportação não divergirem).
    quarto_filtrado = _quarto_id_valido(request.GET.get('quarto')) or ''

    vistorias = Vistoria.objects.select_related('quarto')
    if periodos[periodo_escolhido] is not None:
        vistorias = vistorias.filter(data_vistoria__gte=periodos[periodo_escolhido])
    if quarto_filtrado:
        vistorias = vistorias.filter(quarto_id=quarto_filtrado)

    # Só a tabela do relatório é filtrada.
    relatorios = montar_relatorios(
        relatorios_mod.quartos_com_moradores(_quarto_id_valido(quarto_filtrado)), vistorias)

    # O podio nao herda o `quarto_filtrado`: ele sempre ranqueia o campus
    # inteiro. O filtro acima vale so para a tabela e as metricas do periodo.
    podio_quartos = podio(periodo_escolhido, hoje=data_ref)

    contexto = {
        'quartos_com_moradores': quartos_com_moradores,
        'estudantes': Estudante.objects.select_related('user', 'quarto').order_by('matricula'),
        'relatorios': relatorios,
        'periodo_escolhido': periodo_escolhido,
        'quarto_filtrado': quarto_filtrado,
        'modos_divisao': Quarto.MODOS_DIVISAO,
        'total_quartos': Quarto.objects.count(),
        'total_estudantes': Estudante.objects.count(),
        'total_vistorias_periodo': vistorias.count(),
        'aprovacoes_periodo': vistorias.filter(aprovado=True).count(),
        'podio': podio_quartos,
        'podio_quartos': podio_quartos,
    }
    return render(request, 'gestao_monitor.html', contexto)


def _criar_estudante(request):
    """Cria User + Estudante a partir do formulario de gestao."""
    username = (request.POST.get('username') or '').strip()
    nome = (request.POST.get('nome') or '').strip()
    senha = (request.POST.get('senha') or '').strip()
    matricula = (request.POST.get('matricula') or '').strip()
    cpf = (request.POST.get('cpf') or '').strip()
    quarto_id = request.POST.get('quarto') or ''

    if not username or not matricula or not senha:
        messages.error(request, 'Usuario, matricula e senha sao obrigatorios.')
        return redirect('gestao_monitor')

    if User.objects.filter(username=username).exists():
        messages.error(request, 'Já existe um usuario com esse nome de login.')
        return redirect('gestao_monitor')

    if Estudante.objects.filter(matricula=matricula).exists():
        messages.error(request, 'Já existe um estudante com essa matricula.')
        return redirect('gestao_monitor')

    if cpf and Estudante.objects.filter(cpf=cpf).exists():
        messages.error(request, 'Já existe um estudante com esse CPF.')
        return redirect('gestao_monitor')

    quarto = None
    if quarto_id:
        # .get() e nao .filter().first(): id invalido precisa aparecer como
        # erro, nao virar None silencioso e criar um estudante sem quarto.
        try:
            quarto = Quarto.objects.get(id=quarto_id)
        except (Quarto.DoesNotExist, ValueError):
            messages.error(request, 'Quarto informado nao existe.')
            return redirect('gestao_monitor')

    user = User.objects.create_user(username=username, password=senha, first_name=nome)
    Estudante.objects.create(
        user=user, matricula=matricula, cpf=cpf or None, quarto=quarto,
    )

    messages.success(request, 'Estudante "%s" cadastrado com sucesso!' % username)
    return redirect('gestao_monitor')


def _vincular_estudante_quarto(request):
    """Atualiza o quarto de um estudante ja cadastrado."""
    estudante_id = request.POST.get('estudante') or ''
    quarto_id = request.POST.get('quarto') or ''

    # `pk` e nao `id`: em Estudante a chave primaria e `user` (OneToOne),
    # portanto nao existe coluna `id`.
    estudante = Estudante.objects.filter(pk=estudante_id).select_related('quarto').first()
    if estudante is None:
        messages.error(request, 'Selecione um estudante valido.')
        return redirect('gestao_monitor')

    if not quarto_id:
        # desvincula
        antigo = estudante.quarto
        estudante.quarto = None
        estudante.save(update_fields=['quarto'])
        messages.success(
            request, 'Morador de "%s" desvinculado%s.'
            % (estudante, (' do quarto %s' % antigo) if antigo else '')
        )
        return redirect('gestao_monitor')

    # Reaproveita o quarto ja cadastrado: so troca a FK do estudante.
    # NUNCA cria uma linha nova de Quarto - o mesmo 104 do Bloco B continua
    # sendo um so registro, e so ganha mais um morador.
    try:
        quarto = Quarto.objects.get(id=quarto_id)
    except (Quarto.DoesNotExist, ValueError):
        messages.error(request, 'Selecione um quarto valido.')
        return redirect('gestao_monitor')

    antigo = estudante.quarto
    estudante.quarto = quarto
    estudante.save(update_fields=['quarto'])
    messages.success(
        request, '"%s" agora mora no %s%s.'
        % (estudante, quarto, (' (antes: %s)' % antigo) if antigo else '')
    )
    return redirect('gestao_monitor')


def _salvar_configuracao_quarto(request, quarto):
    """
    Valida e grava os campos de representante/modalidade enviados no POST.

    Campos ausentes sao preservados. Isso permite que o monitor use uma acao
    so para condecorar (sem tocar na modalidade) e outra so para a modalidade
    (sem remover o representante). Quando `representante` vem vazio, ele e
    removido; quando vem preenchido, precisa ser morador do proprio quarto.

    Retorna None em caso de sucesso ou a mensagem de erro. O chamador decide
    para onde redirecionar (gestao do monitor ou painel do estudante).
    """
    modo = request.POST.get('modo_divisao')
    if modo is None:
        modo = quarto.modo_divisao
    elif modo not in dict(Quarto.MODOS_DIVISAO):
        return 'Modalidade de divisao invalida.'

    if 'representante' in request.POST:
        representante_id = request.POST.get('representante') or ''
        representante = None
        if representante_id:
            representante = (
                Estudante.objects
                .filter(pk=representante_id, quarto=quarto)
                .select_related('user')
                .first()
            )
            if representante is None:
                return 'O representante precisa ser um morador do quarto selecionado.'
        quarto.representante = representante

    quarto.modo_divisao = modo
    quarto.save(update_fields=['representante', 'modo_divisao'])
    return None


def _configurar_quarto(request):
    """Salva a configuracao feita pelo monitor na Gestao do Alojamento."""
    quarto_id = request.POST.get('quarto') or ''
    try:
        quarto = Quarto.objects.get(id=quarto_id)
    except (Quarto.DoesNotExist, ValueError):
        messages.error(request, 'Selecione um quarto valido.')
        return redirect('gestao_monitor')

    erro = _salvar_configuracao_quarto(request, quarto)
    if erro:
        messages.error(request, erro)
        return redirect('gestao_monitor')

    messages.success(
        request,
        'Configuracao do %s atualizada (modo %s%s).'
        % (
            quarto,
            quarto.get_modo_divisao_display(),
            (', representante: %s' % quarto.representante)
            if quarto.representante else '',
        ),
    )
    return redirect('gestao_monitor')


def _condecorar_representante(request):
    """Monitor/admin atribui, substitui ou remove o representante do quarto."""
    quarto_id = request.POST.get('quarto') or ''
    try:
        quarto = Quarto.objects.get(id=quarto_id)
    except (Quarto.DoesNotExist, ValueError):
        messages.error(request, 'Selecione um quarto valido.')
        return redirect('gestao_monitor')

    if 'representante' not in request.POST:
        messages.error(request, 'Selecione um representante ou a opcao Nenhum.')
        return redirect('gestao_monitor')

    erro = _salvar_configuracao_quarto(request, quarto)
    if erro:
        messages.error(request, erro)
        return redirect('gestao_monitor')

    if quarto.representante:
        messages.success(
            request,
            'Representante do %s condecorado: %s.'
            % (quarto, quarto.representante),
        )
    else:
        messages.success(
            request,
            'Representante do %s removido. O quarto ficou sem representante.'
            % quarto,
        )
    return redirect('gestao_monitor')


def _configurar_quarto_do_estudante(request, estudante):
    """
    Permite ao representante ativo alterar SOMENTE a modalidade do quarto.

    O representante e uma atribuicao do monitor/admin. Qualquer POST de aluno
    que traga o campo `representante` e tratado como tentativa de escalada e
    recebe 403 antes de tocar no banco. Moradores comuns tambem recebem 403
    ao tentar alterar a modalidade.
    """
    quarto = estudante.quarto
    if quarto is None:
        messages.error(request, 'Voce ainda nao esta vinculado a um quarto.')
        return redirect('painel_estudante')

    if 'representante' in request.POST:
        return HttpResponseForbidden(
            'Somente o monitor ou um administrador pode definir o representante do quarto.'
        )

    if quarto.representante_id != estudante.pk:
        return HttpResponseForbidden(
            'Somente o representante atual do quarto ou o monitor pode alterar a modalidade.'
        )

    if not request.POST.get('modo_divisao'):
        messages.error(request, 'Selecione uma modalidade de divisao.')
        return redirect('painel_estudante')

    erro = _salvar_configuracao_quarto(request, quarto)
    if erro:
        messages.error(request, erro)
    else:
        messages.success(
            request,
            'Modalidade do %s atualizada para %s.'
            % (quarto, quarto.get_modo_divisao_display()),
        )
    return redirect('painel_estudante')


@login_required
@require_POST
def simular_proximo_dia(request):
    """
    Avanca a data da sessao em +1 dia (simulador do Demo Day).

    Nada e gravado no banco: as vistorias e cronogramas sao recalculados
    sob demanda pela data de referencia no proximo request. Assim o historico
    real permanece intacto e cada usuario tem seu proprio calendario.
    """
    nova_data = avancar_dia(request)
    messages.success(
        request,
        'Data simulada avançada para %s.' % nova_data.strftime('%d/%m/%Y'),
    )

    destino = request.META.get('HTTP_REFERER') or reverse('painel_monitor')
    if not url_has_allowed_host_and_scheme(
        destino,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        destino = reverse('painel_monitor')
    return redirect(destino)


@login_required
def gerar_cronograma_view(request):
    """Acao do monitor: dispara o comando que monta o cronograma da proxima semana."""
    if request.method != 'POST':
        return redirect('painel_monitor')

    if not hasattr(request.user, 'monitor'):
        messages.warning(request, 'Apenas monitores podem gerar o cronograma.')
        return redirect('painel_monitor')

    data_ref = data_referencia(request)
    try:
        call_command(
            'gerar_cronograma', verbosity=0, data=data_ref.isoformat(),
        )
    except Exception as erro:
        messages.error(request, 'Não foi possível gerar o cronograma: %s' % erro)
        return redirect('painel_monitor')

    messages.success(request, 'Cronograma da próxima semana gerado com sucesso!')
    return redirect('painel_monitor')


@login_required
def painel_monitor(request):
    if hasattr(request.user, 'monitor'):

        # Apenas estudantes COM quarto: o painel lista quartos para vistoria.
        # Estudante sem quarto quebraria `{% url 'detalhes_quarto' item.quarto.id %}`
        # (item.quarto seria None) com NoReverseMatch / HTTP 500.
        # Quem administra vinculos agora e o painel de Gestao.
        #
        # .distinct() aqui e defensivo: hoje o filtro usa apenas um JOIN de
        # select_related (que nao duplica linhas). Custa pouco e protege caso
        # um filtro com join reverso entre depois.
        #
        # moradores_count e a contagem REAL de moradores do quarto. Antes nao
        # era anotado, entao o template caía no |default:"1" e todo quarto
        # aparecia como tendo 1 morador, mesmo com dois colegas dentro.
        estudantes = (
            Estudante.objects.filter(quarto__isnull=False)
            .select_related('quarto')
            .annotate(
                moradores_count=Count('quarto__estudante', distinct=True),
            )
            .distinct()
        )

        total_pendentes = Tarefa.objects.filter(status='Pendente').count()

        data_ref = data_referencia(request)
        analytics = payload_analytics(hoje=data_ref)
        kpis = analytics['kpis']
        podio_quartos = podio(hoje=data_ref)

        contexto = {
            'estudantes': estudantes,
            'total_pendentes': total_pendentes,
            'total_quartos': estudantes.count(),
            'sem_quarto': Estudante.objects.filter(quarto__isnull=True).count(),
            # Analytics: dict JSON-serializavel. No template vai por
            # |json_script, que escapa o conteudo.
            'analytics': analytics,
            # As quatro chaves tambem ficam no topo do contexto. Os cartoes
            # usam `default`/`floatformat`; None viraria "—" (ou nada).
            'media_nota': (
                kpis['media_nota'] if kpis['media_nota'] is not None else 0.0
            ),
            'aproveitamento': kpis['aproveitamento'],
            'total_vistorias': kpis['total_vistorias'],
            'chamados_abertos': kpis['chamados_abertos'],
            'chamados_total': kpis['chamados_total'],
            'podio': podio_quartos,
            'podio_quartos': podio_quartos,
            # Sidebar: UM item por quarto. `estudantes` continua no contexto
            # porque outros blocos do painel ainda o usam.
            'quartos': quartos_para_painel(),
        }
        return render(request, 'painel_monitor.html', contexto)

        
    elif hasattr(request.user, 'estudante'):
        return redirect('painel_estudante')
    else:
        logout(request)
        messages.error(request, 'Acesso negado.')
        return redirect('login')


@login_required
def qrcode_quarto_svg(request, quarto_id):
    """
    QR Code do quarto, gerado NO SERVIDOR e devolvido em SVG.

    Existe por robustez: a biblioteca JS vinha de CDN e falha justamente
    no notebook em hotspot - o cenario da apresentacao. Aqui nao ha CDN, nem
    JavaScript, nem pacote externo: so o encoder em Python puro.
    """
    if not hasattr(request.user, 'monitor'):
        return HttpResponseForbidden(
            'Acesso restrito: apenas monitores podem gerar QR Code.'
        )

    quarto = get_object_or_404(Quarto, id=quarto_id)

    # URL ABSOLUTA: o QR e lido por um celular, possivelmente de outra rede.
    url_vistoria = request.build_absolute_uri(
        reverse('vistoria_quarto', args=[quarto.id])
    )

    try:
        svg = para_svg(url_vistoria)
    except ValueError:
        return HttpResponseBadRequest('URL longa demais para um QR Code.')

    resposta = HttpResponse(svg, content_type='image/svg+xml')
    resposta['Content-Disposition'] = 'inline; filename="qrcode_quarto_%d.svg"' % quarto.id
    # o conteudo so depende do id do quarto
    resposta['Cache-Control'] = 'private, max-age=3600'
    return resposta


@login_required
def vistoria_quarto(request, quarto_id):
    """
    Destino do QR Code colado na porta do quarto.

    Rota NOVA (o pedido era /monitor/vistoria/<id>/). Ela NAO duplica a logica
    de vistoria: delega para detalhes_quarto, que ja trata monitor/estudante,
    o POST da nota e o redirecionamento. Se um dia a tela de quarto mudar,
    o QR acompanha sozinho.
    """
    return detalhes_quarto(request, quarto_id)


@login_required
def analytics_json(request):
    """Mesmos dados do card de graficos, servidos como JSON."""
    if not hasattr(request.user, 'monitor'):
        return redirect('painel_estudante' if hasattr(request.user, 'estudante') else 'login')

    dias = request.GET.get('dias', 30)
    try:
        dias = max(7, min(365, int(dias)))
    except (TypeError, ValueError):
        dias = 30

    return JsonResponse(
        payload_analytics(dias, hoje=data_referencia(request)), safe=True,
    )


@login_required
def detalhes_quarto(request, quarto_id):
    if hasattr(request.user, 'monitor'):
        quarto_selecionado = get_object_or_404(Quarto, id=quarto_id)
        data_ref = data_referencia(request)

        if request.method == 'POST':
            # Nota calculada a partir dos critérios marcados (0 a 10).
            nota = sum(
                peso
                for criterio, peso in CRITERIOS_VISTORIA.items()
                if request.POST.get(criterio) == '1'
            )

            observacoes = request.POST.get('observacoes', '')
            aprovado = request.POST.get('aprovado') == 'True'

            Vistoria.objects.create(
                monitor=request.user.monitor,
                quarto=quarto_selecionado,
                nota=nota,
                observacoes=observacoes,
                aprovado=aprovado,
                data_vistoria=data_ref,
            )

            if aprovado:
                quarto_selecionado.status = Quarto.STATUS_APROVADO
                quarto_selecionado.streak_dias = (quarto_selecionado.streak_dias or 0) + 1
            else:
                quarto_selecionado.status = Quarto.STATUS_REPROVADO
                quarto_selecionado.streak_dias = 0

            quarto_selecionado.save()

            messages.success(request, 'Vistoria guardada com sucesso!')
            return redirect('detalhes_quarto', quarto_id=quarto_selecionado.id)

        estudantes = Estudante.objects.select_related('quarto').all()
        total_pendentes = Tarefa.objects.filter(status='Pendente').count()

        # Sem estas metricas os cartoes do rodape renderizavam vazios
        # ("—", "%" e contagens em branco) ao abrir /quarto/<id>/.
        # O recorte e o quarto selecionado e o historico inteiro.
        metricas = metricas_quarto(quarto_selecionado)
        analytics = {
            'serie_notas': serie_notas_semanais(
                30, quarto=quarto_selecionado, hoje=data_ref,
            ),
            'categorias': chamados_por_categoria(quarto=quarto_selecionado),
            'kpis': {
                **metricas,
                'periodo_dias': 30,
                'periodo_rotulo': 'Todo o histórico do quarto',
                'filtrado_por_quarto': True,
            },
        }

        # O podio e do CAMPUS, nao do quarto aberto em /quarto/<id>/.
        # `podio()` nunca recebe quarto_id; o recorte do quarto vale apenas
        # para as metricas e para o formulario de vistoria desta pagina.
        podio_quartos = podio(hoje=data_ref)

        contexto = {
            'estudantes': estudantes,
            'total_pendentes': total_pendentes,
            'total_quartos': estudantes.count(),
            'quarto_selecionado': quarto_selecionado,
            'quartos': quartos_para_painel(),
            'analytics': analytics,
            # `podio` e o nome historico usado pelo template; `podio_quartos`
            # torna explicito que a lista e o Top 3 global dos alojamentos.
            'podio': podio_quartos,
            'podio_quartos': podio_quartos,
            # Chaves explicitas no contexto: o template nao depende de
            # `analytics.kpis` para os quatro cartoes.
            'media_nota': metricas['media_nota'],
            'aproveitamento': metricas['aproveitamento'],
            'total_vistorias': metricas['total_vistorias'],
            'chamados_abertos': metricas['chamados_abertos'],
            'chamados_total': metricas['chamados_total'],
            'ultima_vistoria': (
                Vistoria.objects.filter(quarto=quarto_selecionado)
                .order_by('-data_vistoria', '-id')
                .first()
            ),
        }

        return render(request, 'painel_monitor.html', contexto)

    elif hasattr(request.user, 'estudante'):
        return redirect('painel_estudante')
    else:
        logout(request)
        return redirect('login')

@login_required
def chamados_view(request):
    """
    Pagina dedicada de Chamados de Infraestrutura.

    - Estudante: ve e cria apenas os proprios chamados.
    - Monitor: ve todos os chamados e pode marca-los como Verificado.
    """
    if hasattr(request.user, 'monitor'):
        lista = SolicitacaoReparo.objects.select_related('estudante', 'estudante__quarto').order_by('-data_abertura')
        eh_monitor = True
    elif hasattr(request.user, 'estudante'):
        lista = SolicitacaoReparo.objects.filter(
            estudante=request.user.estudante
        ).select_related('estudante', 'estudante__quarto').order_by('-data_abertura')
        eh_monitor = False

        if request.method == 'POST':
            # Mesmos campos do formulario ja existente (titulo/categoria/descricao).
            titulo = request.POST.get('titulo')
            categoria = request.POST.get('categoria')
            descricao = request.POST.get('descricao')

            SolicitacaoReparo.objects.create(
                estudante=request.user.estudante,
                titulo=titulo,
                categoria=categoria,
                descricao=descricao,
                status='Em Aberto',
            )

            messages.success(request, 'Chamado enviado! A manutenção foi notificada.')
            return redirect('chamados')

    else:
        logout(request)
        messages.error(request, 'Acesso negado.')
        return redirect('login')

    contexto = {
        'chamados': lista,
        'eh_monitor': eh_monitor,
        'total_abertos': lista.exclude(status='Verificado').count(),
        'total_verificados': lista.filter(status='Verificado').count(),
    }
    return render(request, 'chamados.html', contexto)


@login_required
def verificar_reparo(request, reparo_id):
    """Monitor confirma que visitou o dano e marcou o chamado como Verificado."""
    if request.method != 'POST':
        return redirect('chamados')

    if not hasattr(request.user, 'monitor'):
        messages.warning(request, 'Apenas monitores podem verificar chamados.')
        return redirect('chamados')

    reparo = get_object_or_404(SolicitacaoReparo, id=reparo_id)
    reparo.status = 'Verificado'
    reparo.save(update_fields=['status'])

    messages.success(request, f'Chamado #{reparo.id} marcado como verificado.')
    return redirect('chamados')

@login_required
def reportar_problema(request):

    if hasattr(request.user, 'estudante'):
        # O formulário em reportar_problema.html posta para 'enviar_relatorio'.
        # Se um POST chegar aqui, delegamos para lá em vez de fingir sucesso.
        if request.method == 'POST':
            return enviar_relatorio(request)

        return render(request, 'reportar_problema.html')

    elif hasattr(request.user, 'monitor'):
        messages.warning(request, 'Monitores não podem reportar problemas por esta tela.')
        return redirect('painel_monitor')

    else:
        return redirect('login')


@login_required
def progresso(request):
    if hasattr(request.user, 'estudante'):
        estudante = request.user.estudante
        quarto = estudante.quarto

        tarefas = Tarefa.objects.filter(
            Q(estudante_responsavel=estudante)
            | Q(estudante_responsavel_2=estudante)
            | Q(estudante_responsavel__isnull=True, cronograma__quarto=quarto)
        ).distinct()
        total_tarefas = tarefas.count()
        tarefas_concluidas = tarefas.filter(status='Concluída').count()
        progresso_percentual = (tarefas_concluidas / total_tarefas * 100) if total_tarefas > 0 else 0

        hoje = timezone.now()
        _, num_dias = calendar.monthrange(hoje.year, hoje.month)
        todos_os_dias = list(range(1, num_dias + 1))

        dias_aprovados = []
        streak_atual = 0
        if quarto is not None:
            vistorias_mes = Vistoria.objects.filter(
                quarto=quarto,
                aprovado=True,
                data_vistoria__year=hoje.year,
                data_vistoria__month=hoje.month
            )
            dias_aprovados = list(vistorias_mes.values_list('data_vistoria__day', flat=True))
            streak_atual = quarto.streak_dias or 0
        else:
            messages.warning(request, 'Você ainda não tem um quarto vinculado. O progresso do quarto ficará indisponível.')

        contexto = {
            'estudante': estudante,
            'quarto': quarto,
            'sem_quarto': quarto is None,
            'tarefas': tarefas,
            'total_tarefas': total_tarefas,
            'tarefas_concluidas': tarefas_concluidas,
            'progresso_percentual': progresso_percentual,
            'hoje': hoje,
            'todos_os_dias': todos_os_dias,
            'dias_aprovados': dias_aprovados,
            'streak_atual': streak_atual
        }

        return render(request, 'progresso.html', contexto)

    elif hasattr(request.user, 'monitor'):
        return redirect('painel_monitor')

    else:
        logout(request)
        messages.error(request, 'Acesso negado.')
        return redirect('login')

@login_required
def enviar_relatorio(request):
    if hasattr(request.user, 'estudante'):
        estudante = request.user.estudante

        if request.method == 'POST':
            titulo = request.POST.get('titulo')
            categoria = request.POST.get('categoria')
            descricao = request.POST.get('descricao')
            # request.FILES so vem preenchido se o form tiver enctype multipart.
            # Sem foto, .get devolve None e o campo opcional fica vazio.
            imagem = request.FILES.get('imagem')

            SolicitacaoReparo.objects.create(
                estudante=estudante,
                titulo=titulo,
                categoria=categoria,
                descricao=descricao,
                status='Em Aberto',
                imagem=imagem,
            )

            messages.success(request, 'Problema reportado com sucesso! A manutenção foi notificada.')
            return redirect('painel_estudante')

        return render(request, 'reportar_problema.html')

    elif hasattr(request.user, 'monitor'):
        messages.warning(request, 'Monitores não podem reportar problemas por esta tela.')
        return redirect('painel_monitor')

    else:
        return redirect('login')


def destino_padrao_perfil(request):
    """Para onde voltar depois de mexer na foto."""
    monitor = getattr(request.user, 'monitor', None)
    return 'painel_monitor' if monitor is not None else 'painel_estudante'


@login_required
def atualizar_foto_perfil(request):
    """
    Upload / troca / remocao da foto de perfil.

    Rota nova: nao encosta em nenhuma view existente. O campo e opcional, entao
    um POST sem arquivo precisa ser um no-op seguro, nao um erro 500.
    """
    monitor = getattr(request.user, 'monitor', None)

    # So aceita voltar para uma pagina do proprio sistema (evita open redirect).
    sugerido = request.POST.get('next')
    destino = sugerido if (sugerido and url_has_allowed_host_and_scheme(sugerido, {request.get_host()})) \
        else ('painel_monitor' if monitor is not None else 'painel_estudante')

    perfil = getattr(request.user, 'estudante', None) or monitor
    if perfil is None or not hasattr(perfil, 'foto_perfil'):
        messages.error(request, 'Sua conta não possui foto de perfil.')
        return redirect(destino)

    if request.method != 'POST':
        return redirect(destino)

    if request.POST.get('acao') == 'remover':
        if perfil.foto_perfil:
            perfil.foto_perfil.delete(save=False)
            perfil.foto_perfil = None
            perfil.save()
        messages.success(request, 'Foto removida. Voltamos a usar as suas iniciais.')
        return redirect(destino)

    foto = request.FILES.get('foto_perfil')
    if not foto:
        messages.warning(request, 'Escolha uma imagem antes de salvar.')
        return redirect(destino)

    if getattr(foto, 'size', 0) > 5 * 1024 * 1024:
        messages.error(request, 'A imagem deve ter no máximo 5 MB.')
        return redirect(destino)

    perfil.foto_perfil.save(foto.name, foto, save=True)
    messages.success(request, 'Foto de perfil atualizada!')
    return redirect(destino)