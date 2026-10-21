# -*- coding: utf-8 -*-
"""
Une quartos duplicados (mesmo numero + bloco) e move os moradores.

Depois da migracao 0008 o banco RECUSA novas duplicatas, entao este comando
existe para bases que ja tinham o problema antes da restricao. Ele mantem o
quarto de menor id, move Estudantes, Vistorias, Cronogramas e Tarefas para
ele, conserva o melhor streak/status e apaga o excedente.

    python manage.py unificar_quartos --simular
    python manage.py unificar_quartos
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from ClearOrder.relatorios import unificar_quartos_duplicados


class _SimulacaoConcluida(Exception):
    """Sinaliza o rollback proposital da simulacao."""


class Command(BaseCommand):
    help = 'Funde quartos duplicados (mesmo numero e bloco), preservando os moradores.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--simular', action='store_true',
            help='apenas mostra o que seria feito, sem alterar o banco',
        )

    def handle(self, *args, **options):
        if options['simular']:
            relatorio = self._simular()
        else:
            relatorio = unificar_quartos_duplicados()

        if not relatorio:
            self.stdout.write(self.style.SUCCESS(
                'Nenhum quarto duplicado encontrado. Nada a fazer.'))
            return

        for numero, bloco, pk in relatorio:
            self.stdout.write('  Quarto %s - Bloco %s: duplicado #%d unido ao quarto mantido'
                             % (numero, bloco, pk))
        self.stdout.write(self.style.SUCCESS(
            '%d quarto(s) duplicado(s) removido(s).' % len(relatorio)))

    def _simular(self):
        """Roda tudo dentro de uma transacao que é revertida ao final."""
        relatorio = []
        try:
            with transaction.atomic():
                relatorio = unificar_quartos_duplicados()
                raise _SimulacaoConcluida()
        except _SimulacaoConcluida:
            pass

        if relatorio:
            self.stdout.write(self.style.WARNING(
                'SIMULACAO: nada foi gravado. %d quarto(s) seria(m) fundido(s).'
                % len(relatorio)))
        return relatorio
