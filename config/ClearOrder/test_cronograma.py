# -*- coding: utf-8 -*-
"""
Testes das modalidades de divisao do cronograma (SOLO/DUPLA/LIVRE).

A funcao `gerar_cronograma_quarto` e o ponto unico usado pelo management
command e pela interface. Estes testes cobrem a regra de atribuicao e a
visibilidade das tarefas no painel do estudante.
"""

import re

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .cronograma import gerar_cronograma_quarto
from .cronograma_utils import TAREFAS_BASE, janela_da_semana
from .models import (
    Cronograma,
    Estudante,
    Monitor,
    Quarto,
    Tarefa,
    Vistoria,
)


class GerarCronogramaQuartoModesTests(TestCase):
    """Regras de atribuicao por modalidade."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='801', bloco='M')
        self.moradores = []
        for indice in range(3):
            user = User.objects.create_user('modo%d' % indice, password='x')
            self.moradores.append(Estudante.objects.create(
                user=user,
                matricula='MODO%d' % indice,
                cpf='7000000000%d' % indice,
                quarto=self.quarto,
            ))
        self.inicio, self.fim = janela_da_semana(offset=0)

    def gerar(self, modo):
        self.quarto.modo_divisao = modo
        self.quarto.save(update_fields=['modo_divisao'])
        return gerar_cronograma_quarto(
            self.quarto, inicio=self.inicio, fim=self.fim,
        )

    def test_solo_mantem_uma_tarefa_por_base(self):
        resultado = self.gerar(Quarto.MODO_SOLO)

        self.assertFalse(resultado['sem_moradores'])
        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE))
        self.assertEqual(
            set(Tarefa.objects.values_list('estudante_responsavel_id', flat=True)),
            {morador.pk for morador in self.moradores},
        )
        self.assertFalse(
            Tarefa.objects.exclude(estudante_responsavel_2__isnull=True).exists()
        )

    def test_dupla_preenche_dois_responsaveis_por_tarefa(self):
        resultado = self.gerar(Quarto.MODO_DUPLA)

        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE))
        contagem = {morador.pk: 0 for morador in self.moradores}
        for tarefa in Tarefa.objects.all():
            self.assertIsNotNone(tarefa.estudante_responsavel_id)
            self.assertIsNotNone(tarefa.estudante_responsavel_2_id)
            self.assertNotEqual(
                tarefa.estudante_responsavel_id,
                tarefa.estudante_responsavel_2_id,
            )
            self.assertEqual(len(tarefa.responsaveis), 2)
            contagem[tarefa.estudante_responsavel_id] += 1
            contagem[tarefa.estudante_responsavel_2_id] += 1

        self.assertTrue(
            all(quantidade == 2 for quantidade in contagem.values()),
            contagem,
        )

    def test_livre_cria_tarefas_abertas(self):
        self.gerar(Quarto.MODO_LIVRE)

        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE))
        for tarefa in Tarefa.objects.all():
            self.assertIsNone(tarefa.estudante_responsavel_id)
            self.assertIsNone(tarefa.estudante_responsavel_2_id)
            self.assertTrue(tarefa.aberta)

    def test_mudanca_de_modo_atualiza_as_mesmas_tarefas(self):
        self.gerar(Quarto.MODO_SOLO)
        ids_solo = set(Tarefa.objects.values_list('id', flat=True))

        resultado = self.gerar(Quarto.MODO_DUPLA)
        ids_dupla = set(Tarefa.objects.values_list('id', flat=True))

        self.assertEqual(ids_solo, ids_dupla, 'a mudanca de modo duplicou tarefas')
        self.assertEqual(resultado['tarefas_criadas'], 0)
        self.assertEqual(resultado['tarefas_atualizadas'], len(TAREFAS_BASE))
        self.assertFalse(
            Tarefa.objects.filter(estudante_responsavel_2__isnull=True).exists()
        )

        # Rodar de novo sem mudar nada nao deve contar atualizacoes.
        resultado = self.gerar(Quarto.MODO_DUPLA)
        self.assertEqual(resultado['tarefas_criadas'], 0)
        self.assertEqual(resultado['tarefas_atualizadas'], 0)

    def test_livre_para_solo_volta_a_designar(self):
        self.gerar(Quarto.MODO_LIVRE)
        self.assertTrue(Tarefa.objects.filter(
            estudante_responsavel__isnull=True).exists())

        self.gerar(Quarto.MODO_SOLO)
        self.assertFalse(Tarefa.objects.filter(
            estudante_responsavel__isnull=True).exists())
        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE))

    def test_quarto_sem_moradores_nao_cria_cronograma(self):
        vazio = Quarto.objects.create(numero='802', bloco='M')
        resultado = gerar_cronograma_quarto(vazio, inicio=self.inicio, fim=self.fim)

        self.assertTrue(resultado['sem_moradores'])
        self.assertIsNone(resultado['cronograma'])
        self.assertFalse(Cronograma.objects.filter(quarto=vazio).exists())
        self.assertFalse(Tarefa.objects.filter(cronograma__quarto=vazio).exists())

    def test_livre_aparece_para_todos_e_primeira_conclusao_assume(self):
        self.gerar(Quarto.MODO_LIVRE)
        tarefa = Tarefa.objects.order_by('id').first()

        for morador in self.moradores:
            cliente = Client(SERVER_NAME='localhost')
            cliente.force_login(morador.user)
            html = cliente.get(reverse('painel_estudante')).content.decode()
            self.assertIn(tarefa.titulo, html)
            self.assertIn('Livre', html)

        # Qualquer morador pode assumir; o primeiro que conclui vira o
        # responsavel registrado.
        cliente = Client(SERVER_NAME='localhost')
        cliente.force_login(self.moradores[1].user)
        resposta = cliente.get(reverse('concluir_tarefa', args=[tarefa.pk]))
        self.assertEqual(resposta.status_code, 302)

        tarefa.refresh_from_db()
        self.assertEqual(tarefa.estudante_responsavel, self.moradores[1])
        self.assertEqual(tarefa.status, 'Concluída')

    def test_dupla_aparece_para_os_dois_responsaveis(self):
        self.gerar(Quarto.MODO_DUPLA)
        tarefa = Tarefa.objects.order_by('id').first()

        for morador in (tarefa.estudante_responsavel,
                        tarefa.estudante_responsavel_2):
            cliente = Client(SERVER_NAME='localhost')
            cliente.force_login(morador.user)
            html = cliente.get(reverse('painel_estudante')).content.decode()
            self.assertIn(tarefa.titulo, html)
            self.assertIn('Dupla', html)


class ResetStatusNovoCicloTests(TestCase):
    """Novo cronograma semanal abre um novo ciclo de vistoria."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='901', bloco='R')
        morador = User.objects.create_user('morador_reset', password='x')
        self.estudante = Estudante.objects.create(
            user=morador,
            matricula='RESET1',
            cpf='55555555555',
            quarto=self.quarto,
        )

        u_monitor = User.objects.create_user('monitor_reset', password='x')
        self.monitor = Monitor.objects.create(
            user=u_monitor,
            siape='7654321',
            cpf='66666666666',
            bloco_responsavel='R',
        )
        self.client.force_login(u_monitor)

    @staticmethod
    def _classes_de(html, elemento_id):
        encontrado = re.search(
            r'id="%s"[^>]*class="([^"]*)"' % elemento_id, html,
        )
        return encontrado.group(1).split() if encontrado else []

    def test_gerar_novo_cronograma_reseta_status_e_preserva_historico(self):
        # Vistoria aprovada no ciclo antigo.
        resposta = self.client.post(
            reverse('detalhes_quarto', args=[self.quarto.id]),
            {
                'chao_lavanderia': '1',
                'organizacao_geral': '1',
                'janelas_livres': '1',
                'observacoes': 'ciclo anterior',
                'aprovado': 'True',
            },
        )
        self.assertEqual(resposta.status_code, 302)

        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.status, Quarto.STATUS_APROVADO)
        vistoria_antiga = Vistoria.objects.get()
        self.assertTrue(vistoria_antiga.aprovado)

        # Botao "Gerar Cronograma Semanal" do painel do monitor.
        resposta = self.client.post(reverse('gerar_cronograma'))
        self.assertEqual(resposta.status_code, 302)

        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.status, Quarto.STATUS_PENDENTE)

        # Historico preservado: a vistoria antiga continua no banco.
        self.assertEqual(Vistoria.objects.count(), 1)
        self.assertTrue(
            Vistoria.objects.filter(
                pk=vistoria_antiga.pk, aprovado=True,
            ).exists()
        )

        # Card do painel com badge amarelo e formulario pronto para nova
        # vistoria, com os criterios desmarcados.
        html_painel = self.client.get(reverse('painel_monitor')).content.decode()
        self.assertIn('bg-amber-100 text-amber-800', html_painel)

        html_quarto = self.client.get(
            reverse('detalhes_quarto', args=[self.quarto.id]),
        ).content.decode()
        self.assertNotIn('hidden', self._classes_de(html_quarto, 'area-botoes'))
        self.assertIn('hidden', self._classes_de(html_quarto, 'area-concluida'))
        # Evita que o navegador restaure checkboxes marcados ao recarregar.
        self.assertIn('autocomplete="off"', html_quarto)
        for criterio in (
            'chao_lavanderia', 'organizacao_geral', 'janelas_livres',
        ):
            self.assertNotRegex(
                html_quarto,
                r'<input[^>]+name="%s"[^>]*\bchecked\b' % criterio,
            )
