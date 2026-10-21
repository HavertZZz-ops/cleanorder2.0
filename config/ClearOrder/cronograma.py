# -*- coding: utf-8 -*-
"""
Geracao do cronograma semanal de um quarto.

A regra de divisao vive na modalidade do Quarto:

- SOLO   -> cada tarefa base tem um morador responsavel (comportamento antigo).
- DUPLA  -> cada tarefa base tem dois moradores responsaveis.
- LIVRE  -> a tarefa nasce sem responsavel e fica aberta para qualquer morador
            do quarto assumir ao concluir.

O comando `gerar_cronograma` e a view que o dispara chamam esta funcao. Assim a
regra de divisao nao fica duplicada entre management command e tela.
"""

from datetime import timedelta

from django.db import transaction

from .cronograma_utils import (
    TAREFAS_BASE,
    deslocamento_do_rodizio,
    janela_da_semana,
)
from .models import Cronograma, Estudante, Quarto, Tarefa


def _designados(estudantes, indice, deslocamento, modo):
    """Devolve (principal, secundario) para uma tarefa base."""
    if modo == Quarto.MODO_LIVRE:
        return None, None

    quantidade = len(estudantes)
    principal = estudantes[(indice + deslocamento) % quantidade]

    if modo != Quarto.MODO_DUPLA:
        return principal, None

    # Com um unico morador nao existe segunda pessoa distinta; repetir o
    # principal mantem a regra "dois campos preenchidos" sem inventar gente.
    secundario = estudantes[(indice + deslocamento + 1) % quantidade]
    return principal, secundario


@transaction.atomic
def gerar_cronograma_quarto(quarto, inicio=None, fim=None, deslocamento=None):
    """
    Gera (ou atualiza) o cronograma de um quarto para a semana informada.

    Idempotente: a chave logica e (cronograma, titulo, data_prazo). Rodar de
    novo na mesma semana atualiza apenas os responsaveis quando a modalidade
    mudou; tarefas ja concluidas preservam status e responsaveis.

    Ao criar um Cronograma novo (inicio de um novo ciclo semanal), o status
    do quarto volta para Pendente. As Vistorias antigas NAO sao apagadas: o
    historico continua disponivel para relatorios, graficos e pódio. Rodar o
    comando de novo na mesma semana nao desfaz uma vistoria ja feita.

    Retorna um dicionario com contadores e a lista de tarefas recem-criadas.
    """
    if inicio is None or fim is None:
        inicio, fim = janela_da_semana(offset=0)
    if deslocamento is None:
        deslocamento = deslocamento_do_rodizio(inicio)

    estudantes = list(
        Estudante.objects.filter(quarto=quarto).order_by('pk')
    )
    if not estudantes:
        return {
            'cronograma': None,
            'cronograma_criado': False,
            'sem_moradores': True,
            'tarefas_criadas': 0,
            'tarefas_existentes': 0,
            'tarefas_atualizadas': 0,
            'status_resetado': False,
            'novas': [],
        }

    cronograma, cronograma_criado = Cronograma.objects.get_or_create(
        quarto=quarto,
        data_inicio=inicio,
        data_fim=fim,
        defaults={'ativo': True},
    )

    # O ciclo semanal e identificado pelo Cronograma. Resetar apenas quando
    # ele e criado evita apagar o resultado de uma vistoria caso o monitor
    # clique em "Gerar Cronograma" de novo na mesma semana.
    status_resetado = False
    if cronograma_criado and quarto.status != Quarto.STATUS_PENDENTE:
        quarto.status = Quarto.STATUS_PENDENTE
        quarto.save(update_fields=['status'])
        status_resetado = True

    modo = quarto.modo_divisao
    if modo not in dict(Quarto.MODOS_DIVISAO):
        modo = Quarto.MODO_SOLO

    criadas = 0
    existentes = 0
    atualizadas = 0
    novas = []

    for indice, (titulo, descricao) in enumerate(TAREFAS_BASE):
        principal, secundario = _designados(
            estudantes, indice, deslocamento, modo,
        )
        prazo = inicio + timedelta(days=indice % 7)

        # A busca NAO inclui o responsavel: a tarefa e identificada pelo
        # titulo e pelo prazo dentro do cronograma. Isso evita duplicar a
        # mesma tarefa quando a modalidade do quarto muda.
        tarefa = (
            Tarefa.objects
            .filter(cronograma=cronograma, titulo=titulo, data_prazo=prazo)
            .order_by('id')
            .first()
        )

        if tarefa is None:
            tarefa = Tarefa.objects.create(
                cronograma=cronograma,
                estudante_responsavel=principal,
                estudante_responsavel_2=secundario,
                titulo=titulo,
                descricao=descricao,
                data_prazo=prazo,
                status='Pendente',
            )
            criadas += 1
            novas.append(tarefa)
            continue

        existentes += 1
        if tarefa.status == 'Concluída':
            continue

        alterou = False
        if tarefa.estudante_responsavel_id != (
            principal.pk if principal is not None else None
        ):
            tarefa.estudante_responsavel = principal
            alterou = True
        if tarefa.estudante_responsavel_2_id != (
            secundario.pk if secundario is not None else None
        ):
            tarefa.estudante_responsavel_2 = secundario
            alterou = True
        if not tarefa.descricao and descricao:
            tarefa.descricao = descricao
            alterou = True

        if alterou:
            tarefa.save(update_fields=[
                'estudante_responsavel',
                'estudante_responsavel_2',
                'descricao',
            ])
            atualizadas += 1

    return {
        'cronograma': cronograma,
        'cronograma_criado': cronograma_criado,
        'sem_moradores': False,
        'tarefas_criadas': criadas,
        'tarefas_existentes': existentes,
        'tarefas_atualizadas': atualizadas,
        'status_resetado': status_resetado,
        'novas': novas,
    }
