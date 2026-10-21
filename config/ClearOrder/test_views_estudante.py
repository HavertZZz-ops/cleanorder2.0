# -*- coding: utf-8 -*-
"""Regras de representante/modalidade no painel do estudante."""

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import Estudante, Monitor, Quarto


class PermissoesRepresentanteTests(TestCase):
    """O aluno so altera a modalidade; o representante e atribuido na Gestao."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='201', bloco='T')
        self.outro_quarto = Quarto.objects.create(numero='202', bloco='T')
        self.representante = self.criar_estudante('aluno_rep', 'T201', self.quarto)
        self.colega = self.criar_estudante('aluno_colega', 'T202', self.quarto)
        self.intruso = self.criar_estudante('aluno_intruso', 'T203', self.outro_quarto)
        self.client = Client(SERVER_NAME='localhost')

    def criar_estudante(self, username, matricula, quarto):
        return Estudante.objects.create(
            user=User.objects.create_user(username, password='x'),
            matricula=matricula,
            quarto=quarto,
        )

    def entrar(self, estudante):
        self.client.force_login(estudante.user)

    def definir_representante(self, estudante):
        self.quarto.representante = estudante
        self.quarto.save(update_fields=['representante'])

    def test_estudante_comum_nao_altera_representante(self):
        self.entrar(self.colega)

        resposta = self.client.post(reverse('painel_estudante'), {
            'acao': 'configurar_quarto',
            'representante': str(self.representante.pk),
            'modo_divisao': Quarto.MODO_DUPLA,
        })

        self.assertEqual(resposta.status_code, 403)
        self.quarto.refresh_from_db()
        self.assertIsNone(self.quarto.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_SOLO)

    def test_representante_ativo_altera_somente_a_modalidade(self):
        self.definir_representante(self.representante)
        self.entrar(self.representante)

        resposta = self.client.post(reverse('painel_estudante'), {
            'acao': 'configurar_quarto',
            'modo_divisao': Quarto.MODO_DUPLA,
        })

        self.assertEqual(resposta.status_code, 302)
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.representante, self.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_DUPLA)

    def test_estudante_comum_nao_altera_modalidade(self):
        self.definir_representante(self.representante)
        self.entrar(self.colega)

        resposta = self.client.post(reverse('painel_estudante'), {
            'acao': 'configurar_quarto',
            'modo_divisao': Quarto.MODO_LIVRE,
        })

        self.assertEqual(resposta.status_code, 403)
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.representante, self.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_SOLO)

    def test_representante_nao_pode_trocar_o_proprio_representante(self):
        self.definir_representante(self.representante)
        self.entrar(self.representante)

        resposta = self.client.post(reverse('painel_estudante'), {
            'acao': 'configurar_quarto',
            'representante': str(self.colega.pk),
            'modo_divisao': Quarto.MODO_LIVRE,
        })

        self.assertEqual(resposta.status_code, 403)
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.representante, self.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_SOLO)

    def test_troca_de_representante_atualiza_permissoes_imediatamente(self):
        self.definir_representante(self.representante)
        monitor_user = User.objects.create_user('mon_permissoes', password='x')
        Monitor.objects.create(
            user=monitor_user, siape='6543210', cpf='12345678901',
            bloco_responsavel='T',
        )
        self.client.force_login(monitor_user)

        resposta = self.client.post(reverse('gestao_monitor'), {
            'acao': 'condecorar_representante',
            'quarto': str(self.quarto.pk),
            'representante': str(self.colega.pk),
        })
        self.assertEqual(resposta.status_code, 302)
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.representante, self.colega)

        self.entrar(self.representante)
        antigo = self.client.post(reverse('painel_estudante'), {
            'acao': 'configurar_quarto',
            'modo_divisao': Quarto.MODO_DUPLA,
        })
        self.assertEqual(antigo.status_code, 403)

        self.entrar(self.colega)
        novo = self.client.post(reverse('painel_estudante'), {
            'acao': 'configurar_quarto',
            'modo_divisao': Quarto.MODO_DUPLA,
        })
        self.assertEqual(novo.status_code, 302)
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.representante, self.colega)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_DUPLA)

    def test_painel_mostra_os_tres_estados_de_representacao(self):
        self.entrar(self.colega)
        sem_representante = self.client.get(reverse('painel_estudante')).content.decode()
        self.assertIn('Representante: Nenhum condecorado ainda.', sem_representante)
        self.assertNotIn('name="representante"', sem_representante)
        self.assertNotIn('name="modo_divisao"', sem_representante)
        self.assertNotIn('Salvar Modalidade', sem_representante)

        self.definir_representante(self.representante)
        self.quarto.modo_divisao = Quarto.MODO_DUPLA
        self.quarto.save(update_fields=['modo_divisao'])

        self.entrar(self.representante)
        representante = self.client.get(reverse('painel_estudante')).content.decode()
        self.assertIn('Voc\u00ea \u00e9 o Representante do Quarto', representante)
        self.assertIn('name="modo_divisao"', representante)
        self.assertIn('Salvar Modalidade', representante)
        self.assertNotIn('name="representante"', representante)

        self.entrar(self.colega)
        comum = self.client.get(reverse('painel_estudante')).content.decode()
        self.assertIn('Representante: %s' % self.representante, comum)
        self.assertIn('Modo: Dupla.', comum)
        self.assertNotIn('name="representante"', comum)
        self.assertNotIn('name="modo_divisao"', comum)
        self.assertNotIn('Salvar Modalidade', comum)
