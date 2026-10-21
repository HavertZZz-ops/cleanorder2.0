
from .data_simulada import data_referencia
from .models import Notificacao

LIMITE_RECENTES = 5


def notificacoes(request):
    
    vazio = {
        'qtd_notificacoes': 0,
        'notificacoes_nao_lidas': Notificacao.objects.none(),
        'notificacoes_recentes': Notificacao.objects.none(),
    }

    usuario = getattr(request, 'user', None)
    if usuario is None or not usuario.is_authenticated:
        return vazio

    
    do_usuario = Notificacao.objects.filter(estudante_destinatario__user=usuario)
    nao_lidas = do_usuario.filter(lida=False)

    return {
        'qtd_notificacoes': nao_lidas.count(),
        'notificacoes_nao_lidas': nao_lidas,
        'notificacoes_recentes': do_usuario.order_by('-data_envio', '-id')[:LIMITE_RECENTES],
    }


def data_atual(request):
    """Data da topbar: simulada da sessao ou a data local real."""
    return {'data_atual': data_referencia(request)}


def _mascara_cpf(cpf):
    """12345678909 -> 123.456.789-09. Nao mexe em valor fora do formato."""
    cpf = (cpf or '').strip()
    if len(cpf) == 11 and cpf.isdigit():
        return '%s.%s.%s-%s' % (cpf[:3], cpf[3:6], cpf[6:9], cpf[9:])
    return cpf


def perfil(request):
    """
    Dados do perfil institucional para o modal de perfil.

    Seguro para QUALQUER usuario: usuario sem perfil (administrador) recebe
    None em vez de AttributeError/DoesNotExist.

    O accessor reverso de um OneToOne (user.estudante) levanta
    RelatedObjectDoesNotExist, que herda de AttributeError - por isso
    getattr(..., None) devolve None em vez de explodir.
    """
    vazio = {
        'perfil_estudante': None,
        'perfil_monitor': None,
        'perfil_cpf': '',
        'perfil_quarto_ouro': False,
    }

    usuario = getattr(request, 'user', None)
    if usuario is None or not usuario.is_authenticated:
        return vazio

    # prioridade: estudante > monitor > nenhum
    estudante = getattr(usuario, 'estudante', None)
    monitor = getattr(usuario, 'monitor', None)
    perfil = estudante or monitor

    # Badge do Quarto Ouro. Import local: evita ciclo entre modulos.
    quarto_ouro = False
    if estudante is not None:
        try:
            from .gamificacao import eh_quarto_ouro
            quarto_ouro = eh_quarto_ouro(estudante.quarto)
        except Exception:
            quarto_ouro = False   # gamificacao nunca pode derrubar a pagina

    return {
        'perfil_estudante': estudante,
        'perfil_monitor': monitor,
        'perfil_cpf': _mascara_cpf(getattr(perfil, 'cpf', None)),
        'perfil_quarto_ouro': quarto_ouro,
    }
