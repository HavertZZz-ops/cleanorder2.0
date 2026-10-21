# -*- coding: utf-8 -*-
"""
Regressao: metricas do rodape da pagina /quarto/<id>/.

Antes da correcao, `detalhes_quarto` nao colocava `analytics` (nem as quatro
chaves) no contexto. Os cartoes "Media de Nota", "Aproveitamento",
"Vistorias" e "Chamados Abertos" apareciam com "—", "%" solto e contagens
em branco, mesmo com o quarto tendo vistorias e chamados.
"""
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Estudante, Monitor, Quarto, SolicitacaoReparo, Vistoria


class MetricasQuartoFiltradoTests(TestCase):
    """As quatro metricas precisam refletir SOMENTE o quarto selecionado."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='104', bloco='B')
        self.outro_quarto = Quarto.objects.create(numero='105', bloco='B')

        u_mon = User.objects.create_user('mon_metricas', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='8887776', cpf='11122233344',
            bloco_responsavel='B',
        )

        self.estudante = Estudante.objects.create(
            user=User.objects.create_user('est_metricas', password='x'),
            matricula='MET1', cpf='55566677788', quarto=self.quarto,
        )
        Estudante.objects.create(
            user=User.objects.create_user('est_outro', password='x'),
            matricula='MET2', cpf='99988877766', quarto=self.outro_quarto,
        )

        # Quarto selecionado: 1 vistoria nota 10, aprovada.
        self.vistoria_quarto = Vistoria.objects.create(
            monitor=self.monitor, quarto=self.quarto,
            nota=10, aprovado=True,
        )

        # Outro quarto: dados que NAO podem vazar para os cartoes do 104.
        Vistoria.objects.create(
            monitor=self.monitor, quarto=self.outro_quarto,
            nota=5, aprovado=False,
        )

        # 1 chamado aberto + 1 verificado no 104; 1 aberto no 105.
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='Lampada queimada',
            descricao='d', categoria='eletrica', status='Em Aberto',
        )
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='Torneira consertada',
            descricao='d', categoria='hidraulica', status='Verificado',
        )
        SolicitacaoReparo.objects.create(
            estudante=Estudante.objects.get(matricula='MET2'),
            titulo='Cadeira quebrada', descricao='d',
            categoria='mobiliario', status='Em Aberto',
        )

        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.monitor.user)

    # ---------- helpers ----------
    def abrir_quarto(self, quarto=None):
        return self.client.get(
            reverse('detalhes_quarto', args=[(quarto or self.quarto).pk])
        )

    @staticmethod
    def valor_kpi(html, chave):
        """Texto do cartao marcado com data-kpi="<chave>"."""
        encontrado = re.search(
            r'data-kpi="%s"[^>]*>(.*?)</p>' % re.escape(chave),
            html, re.S,
        )
        if encontrado is None:
            raise AssertionError('cartao %r nao foi renderizado' % chave)
        return re.sub(r'<[^>]+>', ' ', encontrado.group(1)).strip()

    # ---------- contexto ----------
    def test_contexto_tem_as_quatro_chaves_explicitas(self):
        r = self.abrir_quarto()
        self.assertEqual(r.status_code, 200)
        for chave in ('media_nota', 'aproveitamento',
                      'total_vistorias', 'chamados_abertos'):
            self.assertIn(chave, r.context, 'chave ausente no contexto: %s' % chave)

        self.assertEqual(r.context['media_nota'], 10.0)
        self.assertEqual(r.context['aproveitamento'], 100)
        self.assertEqual(r.context['total_vistorias'], 1)
        self.assertEqual(r.context['chamados_abertos'], 1)

    def test_analytics_do_contexto_tambem_esta_filtrado(self):
        r = self.abrir_quarto()
        kpis = r.context['analytics']['kpis']
        self.assertEqual(kpis['media_nota'], 10.0)
        self.assertEqual(kpis['aproveitamento'], 100)
        self.assertEqual(kpis['total_vistorias'], 1)
        self.assertEqual(kpis['chamados_abertos'], 1)
        # A media semanal e a rosca tambem sao do quarto, nao do alojamento.
        self.assertTrue(all(
            ponto['total'] <= 1
            for ponto in r.context['analytics']['serie_notas']
        ))

    # ---------- HTML ----------
    def test_html_mostra_os_quatro_valores_numericos(self):
        html = self.abrir_quarto().content.decode()

        valores = {
            'media-nota': '10.0',
            'aproveitamento': '100%',
            'total-vistorias': '1',
            'chamados-abertos': '1',
        }
        for chave, esperado in valores.items():
            self.assertEqual(self.valor_kpi(html, chave), esperado,
                             'cartao %r com valor errado' % chave)

        # Nenhum cartao pode voltar ao estado quebrado da regressao.
        for chave in valores:
            valor = self.valor_kpi(html, chave)
            self.assertTrue(valor, 'cartao %r vazio' % chave)
            self.assertNotIn('—', valor, 'cartao %r com traco vazio' % chave)
        self.assertNotEqual(self.valor_kpi(html, 'aproveitamento'), '%')

    def test_metricas_de_outro_quarto_nao_vazam(self):
        html = self.abrir_quarto().content.decode()
        self.assertEqual(self.valor_kpi(html, 'total-vistorias'), '1')
        self.assertEqual(self.valor_kpi(html, 'media-nota'), '10.0')
        # Se o filtro falhasse, a media geral seria 7.5 e o total 2.
        self.assertNotEqual(self.valor_kpi(html, 'media-nota'), '7.5')

    def test_rota_do_qr_tambem_mostra_as_metricas(self):
        r = self.client.get(reverse('vistoria_quarto', args=[self.quarto.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['media_nota'], 10.0)
        self.assertEqual(r.context['chamados_abertos'], 1)

    def test_podio_e_global_mesmo_com_quarto_selecionado(self):
        """Abrir /quarto/<id>/ nao pode reduzir o podio ao quarto filtrado."""
        terceiro = Quarto.objects.create(numero='106', bloco='B')
        Vistoria.objects.create(
            monitor=self.monitor, quarto=terceiro,
            nota=8, aprovado=True,
        )

        r = self.abrir_quarto()
        self.assertEqual(r.status_code, 200)
        self.assertIn('podio_quartos', r.context)

        ranking = r.context['podio_quartos']
        self.assertEqual(len(ranking), 3)
        self.assertEqual(
            [linha['quarto'].pk for linha in ranking],
            [self.quarto.pk, terceiro.pk, self.outro_quarto.pk],
        )

        # O podio global nao pode cair no empty state por causa do filtro.
        html = r.content.decode()
        self.assertNotIn('Nenhum quarto disputa o pódio ainda', html)

    # ---------- fallbacks e casos de borda ----------
    def test_quarto_sem_vistorias_usa_zero_e_nao_traco(self):
        vazio = Quarto.objects.create(numero='303', bloco='C')
        r = self.abrir_quarto(vazio)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['media_nota'], 0.0)
        self.assertEqual(r.context['aproveitamento'], 0)
        self.assertEqual(r.context['total_vistorias'], 0)
        self.assertEqual(r.context['chamados_abertos'], 0)

        html = r.content.decode()
        self.assertEqual(self.valor_kpi(html, 'media-nota'), '0.0')
        self.assertEqual(self.valor_kpi(html, 'aproveitamento'), '0%')
        self.assertEqual(self.valor_kpi(html, 'total-vistorias'), '0')
        self.assertEqual(self.valor_kpi(html, 'chamados-abertos'), '0')
        for chave in ('media-nota', 'aproveitamento',
                      'total-vistorias', 'chamados-abertos'):
            self.assertNotIn('—', self.valor_kpi(html, chave))

    def test_vistoria_sem_nota_conta_no_total_mas_nao_na_media(self):
        Vistoria.objects.create(
            monitor=self.monitor, quarto=self.quarto,
            nota=None, aprovado=False,
        )
        r = self.abrir_quarto()
        self.assertEqual(r.context['media_nota'], 10.0)
        self.assertEqual(r.context['total_vistorias'], 2)
        self.assertEqual(r.context['aproveitamento'], 50)

    def test_aproveitamento_usa_todas_as_vistorias_do_quarto(self):
        Vistoria.objects.create(
            monitor=self.monitor, quarto=self.quarto,
            nota=6, aprovado=False,
        )
        r = self.abrir_quarto()
        self.assertEqual(r.context['media_nota'], 8.0)
        self.assertEqual(r.context['aproveitamento'], 50)
        self.assertEqual(r.context['total_vistorias'], 2)

        html = r.content.decode()
        self.assertEqual(self.valor_kpi(html, 'media-nota'), '8.0')
        self.assertEqual(self.valor_kpi(html, 'aproveitamento'), '50%')
        self.assertEqual(self.valor_kpi(html, 'total-vistorias'), '2')

    def test_status_em_aberto_com_codigo_do_enum_tambem_conta(self):
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='Chamado legado',
            descricao='d', categoria='outros', status='EM_ABERTO',
        )
        r = self.abrir_quarto()
        self.assertEqual(r.context['chamados_abertos'], 2)

class TopbarMonitorTests(TestCase):
    """A topbar de /monitor/, /chamados/ e /monitor/gestao/ deve ser a mesma."""

    def setUp(self):
        user = User.objects.create_user('mon_topbar', password='x')
        Monitor.objects.create(
            user=user, siape='7776665', cpf='12312312399',
            bloco_responsavel='B',
        )
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(user)

    def test_data_atual_e_icones_nas_tres_telas_do_monitor(self):
        hoje = timezone.localdate().strftime('%d/%m/%Y')

        for nome in ('painel_monitor', 'chamados', 'gestao_monitor'):
            with self.subTest(tela=nome):
                html = self.client.get(reverse(nome)).content.decode()

                self.assertIn('Data Atual: %s' % hoje, html)
                self.assertIn(
                    'px-4 py-1.5 rounded-2xl bg-slate-100/80', html,
                    'a pilula de data precisa usar o container padrao',
                )
                self.assertIn('aria-label="Notificações"', html)
                self.assertIn('aria-label="Abrir meu perfil"', html)
                self.assertIn('aria-label="Alternar tema claro e escuro"', html)


class SimuladorDataTests(TestCase):
    """POST /monitor/simular-data/ avanca a data da sessao em +1 dia."""

    def setUp(self):
        user = User.objects.create_user('mon_simulador', password='x')
        Monitor.objects.create(
            user=user, siape='5554443', cpf='98765432100',
            bloco_responsavel='B',
        )
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(user)

    def test_post_avanca_data_da_sessao_e_redireciona(self):
        amanha = timezone.localdate() + timedelta(days=1)

        resposta = self.client.post(
            reverse('simular_proximo_dia'),
            HTTP_REFERER=reverse('painel_monitor'),
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(resposta.url, reverse('painel_monitor'))
        self.assertEqual(
            self.client.session.get('data_simulada'), amanha.isoformat(),
        )

        html = self.client.get(reverse('painel_monitor')).content.decode()
        self.assertIn('Data Atual: %s' % amanha.strftime('%d/%m/%Y'), html)
        self.assertIn('⏩ +1 Dia', html)

    def test_dois_posts_incrementam_dois_dias(self):
        alvo = timezone.localdate() + timedelta(days=2)

        self.client.post(reverse('simular_proximo_dia'))
        self.client.post(reverse('simular_proximo_dia'))

        self.assertEqual(
            self.client.session.get('data_simulada'), alvo.isoformat(),
        )

    def test_get_nao_altera_a_data(self):
        resposta = self.client.get(reverse('simular_proximo_dia'))

        self.assertEqual(resposta.status_code, 405)
        self.assertNotIn('data_simulada', self.client.session)


class CondecoracaoRepresentanteTests(TestCase):
    """A Gestao atribui/substitui representante sem tocar na modalidade."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='301', bloco='G')
        self.primeiro = self.criar_estudante('mon_rep_a', 'G301')
        self.segundo = self.criar_estudante('mon_rep_b', 'G302')
        user = User.objects.create_user('mon_gestao_rep', password='x')
        Monitor.objects.create(
            user=user, siape='7654321', cpf='76543210987',
            bloco_responsavel='G',
        )
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(user)

    def criar_estudante(self, username, matricula):
        return Estudante.objects.create(
            user=User.objects.create_user(username, password='x'),
            matricula=matricula,
            quarto=self.quarto,
        )

    def test_gestao_expoe_botao_dropdown_e_opcao_nenhum(self):
        html = self.client.get(reverse('gestao_monitor')).content.decode()

        self.assertIn('Condecorar Representante', html)
        self.assertIn('<option value="">Nenhum</option>', html)
        self.assertIn('name="representante"', html)
        self.assertIn('name="modo_divisao"', html)

    def test_monitor_atribui_substitui_e_remove_representante(self):
        resposta = self.client.post(reverse('gestao_monitor'), {
            'acao': 'condecorar_representante',
            'quarto': str(self.quarto.pk),
            'representante': str(self.primeiro.pk),
        })
        self.assertEqual(resposta.status_code, 302)
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.representante, self.primeiro)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_SOLO)

        self.client.post(reverse('gestao_monitor'), {
            'acao': 'condecorar_representante',
            'quarto': str(self.quarto.pk),
            'representante': str(self.segundo.pk),
        })
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.representante, self.segundo)

        self.client.post(reverse('gestao_monitor'), {
            'acao': 'condecorar_representante',
            'quarto': str(self.quarto.pk),
            'representante': '',
        })
        self.quarto.refresh_from_db()
        self.assertIsNone(self.quarto.representante)

    def test_admin_tambem_pode_condecorar_representante(self):
        admin = User.objects.create_superuser('admin_rep', password='x')
        self.client.force_login(admin)

        resposta = self.client.post(reverse('gestao_monitor'), {
            'acao': 'condecorar_representante',
            'quarto': str(self.quarto.pk),
            'representante': str(self.segundo.pk),
        })

        self.assertEqual(resposta.status_code, 302)
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.representante, self.segundo)
