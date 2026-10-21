from datetime import timedelta
from django.utils import timezone

TAREFAS_BASE = [
    (
        "Varrer e passar pano no chão",
        "Varrer todo o piso do quarto e passar pano de chão, incluindo embaixo das camas.",
    ),
    (
        "Organizar áreas comuns e bancadas",
        "Organizar bancadas, estantes e áreas comuns, deixando tudo no lugar.",
    ),
    (
        "Tirar o lixo e higienizar lixeiras",
        "Retirar o lixo do quarto e higienizar a lixeira.",
    ),
]


def janela_da_semana(offset=0, hoje=None):

    base = hoje or timezone.localdate()
    inicio = base + timedelta(weeks=offset)
    # .weekday(): 0=segunda ... 6=domingo
    inicio -= timedelta(days=inicio.weekday())
    return inicio, inicio + timedelta(days=6)


def deslocamento_do_rodizio(inicio):
    """
    Deslocamento do rodizio em funcao da semana.

    Sem isso o `estudantes[index % len]` repetiria sempre a mesma atribuicao
    toda semana (o morador 0 receberia sempre a tarefa 0) - isso nao e rodizio.
    Como a semana ISO muda a cada semana, o primeiro responsavel tambem muda.
    """
    return inicio.isocalendar()[1]