# -*- coding: utf-8 -*-
"""
Comando: python manage.py gerar_cronograma

Cria o cronograma de limpeza da SEMANA ATUAL (segunda a domingo, ja comecada) e
distribui as tarefas base entre os moradores de cada quarto, de forma rotativa.

Padrao = semana atual, para que as tarefas aparecam imediatamente no painel do
estudante. Use --semana 1 para a proxima semana, --semana 2 para a seguinte.

Sobre a prevencao de duplicados - nota importante:
a spec original pedia `Cronograma.objects.get_or_create(quarto=, estudante=,
tarefa=, prazo=)`, mas a model Cronograma NAO tem os campos `estudante`,
`tarefa` nem `prazo` (eles ficam em Tarefa). A chamada literal levantaria
FieldError. A implementacao real vive em `ClearOrder.cronograma`:

    Cronograma.get_or_create(quarto, data_inicio, data_fim)   -> 1 por quarto/semana
    Tarefa.get(cronograma, titulo, data_prazo)                -> chave logica da tarefa

A modalidade do Quarto (SOLO/DUPLA/LIVRE) decide quantos responsaveis cada
tarefa recebe; o comando apenas percorre os quartos e usa a mesma funcao da
tela de gestao.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ClearOrder.cronograma import gerar_cronograma_quarto
from ClearOrder.cronograma_utils import (
    deslocamento_do_rodizio,
    janela_da_semana,
)
from ClearOrder.models import Cronograma, Quarto


class Command(BaseCommand):
    help = (
        "Gera as tarefas de limpeza da SEMANA ATUAL (segunda a domingo), "
        "distribuidas por rodizio entre os moradores de cada quarto. "
        "Idempotente: rodar duas vezes nao duplica nada."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--semana",
            type=int,
            default=0,
            help="Quantas semanas somar a partir da atual. 0 = semana atual (padrao), 1 = proxima.",
        )
        parser.add_argument(
            "--data",
            default=None,
            help=(
                "Data de referencia no formato YYYY-MM-DD. Padrao: data local "
                "real. O simulador de Demo Day envia a data da sessao."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        offset = options["semana"]
        data_bruta = options.get("data")
        if data_bruta:
            try:
                data_ref = date.fromisoformat(str(data_bruta))
            except (TypeError, ValueError):
                raise CommandError(
                    "--data deve estar no formato YYYY-MM-DD."
                )
        else:
            data_ref = timezone.localdate()

        inicio, fim = janela_da_semana(offset=offset, hoje=data_ref)
        deslocamento = deslocamento_do_rodizio(inicio)

        # Ciclo novo = ainda nao existe nenhum Cronograma nesta janela. Nesse
        # momento TODOS os quartos voltam a Pendente, inclusive os que estao
        # sem moradores. Rodar o comando de novo na mesma semana nao mexe no
        # status de vistorias ja realizadas.
        ciclo_novo = not Cronograma.objects.filter(
            data_inicio=inicio, data_fim=fim,
        ).exists()
        status_resetados = 0
        if ciclo_novo:
            status_resetados = (
                Quarto.objects
                .exclude(status=Quarto.STATUS_PENDENTE)
                .update(status=Quarto.STATUS_PENDENTE)
            )

        self.stdout.write(
            "Gerando cronograma da semana de %s a %s (rodizio +%d)" % (inicio, fim, deslocamento)
        )
        if ciclo_novo:
            self.stdout.write(
                "  status: %d quarto(s) resetado(s) para Pendente" % status_resetados
            )

        cronogramas_criados = 0
        tarefas_criadas = 0
        tarefas_ja_existentes = 0
        quartos_com_moradores = 0
        quartos_sem_moradores = 0

        for quarto in Quarto.objects.all().order_by("id"):
            resultado = gerar_cronograma_quarto(
                quarto,
                inicio=inicio,
                fim=fim,
                deslocamento=deslocamento,
            )

            # Quarto sem moradores e ignorado pela funcao. Sem este guarda,
            # o rodizio levantaria ZeroDivisionError.
            if resultado['sem_moradores']:
                quartos_sem_moradores += 1
                self.stdout.write("  - %s: sem moradores, ignorado" % quarto)
                continue

            quartos_com_moradores += 1
            if resultado['cronograma_criado']:
                cronogramas_criados += 1
            tarefas_criadas += resultado['tarefas_criadas']
            tarefas_ja_existentes += resultado['tarefas_existentes']

            for tarefa in resultado['novas']:
                responsaveis = ', '.join(
                    str(pessoa) for pessoa in tarefa.responsaveis
                ) or 'livre'
                self.stdout.write("  + %s | %s" % (tarefa.titulo, responsaveis))

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "Concluido: %d quarto(s) com moradores, %d cronograma(s) novo(s), "
                "%d tarefa(s) criada(s), %d ja existente(s), %d quarto(s) ignorado(s)."
                % (
                    quartos_com_moradores,
                    cronogramas_criados,
                    tarefas_criadas,
                    tarefas_ja_existentes,
                    quartos_sem_moradores,
                )
            )
        )