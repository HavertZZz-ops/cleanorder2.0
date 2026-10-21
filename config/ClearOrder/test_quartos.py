# -*- coding: utf-8 -*-
"""
Testes do model Quarto: unicidade de (numero, bloco) e a property `andar`.

Arquivo separado de proposito - sao invariantes do MODEL, que nao dependem
de nenhuma tela nem view.
"""
from django.db import IntegrityError, connection, models, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone

from .models import Estudante, Monitor, Quarto, Vistoria
from .relatorios import unificar_quartos_duplicados


class UnicidadeQuartoTests(TestCase):
    """(numero, bloco) identifica um quarto."""

    def test_nao_permite_o_mesmo_numero_no_mesmo_bloco(self):
        Quarto.objects.create(numero='104', bloco='B')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Quarto.objects.create(numero='104', bloco='B')

    def test_permite_o_mesmo_numero_em_blocos_diferentes(self):
        a = Quarto.objects.create(numero='104', bloco='A')
        b = Quarto.objects.create(numero='104', bloco='B')
        self.assertNotEqual(a.pk, b.pk)
        self.assertEqual(Quarto.objects.filter(numero='104').count(), 2)

    def test_obter_ou_criar_reaproveita(self):
        primeiro, criado1 = Quarto.obter_ou_criar('104', 'B')
        segundo, criado2 = Quarto.obter_ou_criar('104', 'B')
        self.assertTrue(criado1)
        self.assertFalse(criado2, 'a segunda chamada NAO deveria criar outro quarto')
        self.assertEqual(primeiro.pk, segundo.pk)
        self.assertEqual(Quarto.objects.count(), 1)

    def test_obter_ou_criar_cria_quando_nao_existe(self):
        _, criado = Quarto.obter_ou_criar('999', 'Z')
        self.assertTrue(criado)

    def test_obter_ou_criar_exige_numero_e_bloco(self):
        with self.assertRaises(ValueError):
            Quarto.obter_ou_criar('', 'B')
        with self.assertRaises(ValueError):
            Quarto.obter_ou_criar('104', '')


class AndarPropertyTests(TestCase):
    """A property `andar` deriva o andar do primeiro digito do numero."""

    def test_andar_por_casa(self):
        casos = [
            ('104', '1º andar'),
            ('202', '2º andar'),
            ('305', '3º andar'),
            ('410', '4º andar'),
        ]
        for numero, esperado in casos:
            with self.subTest(numero=numero):
                self.assertEqual(Quarto(numero=numero, bloco='B').andar, esperado)

    def test_andar_em_quarto_persistido(self):
        for numero, esperado in [('104', '1º andar'), ('202', '2º andar'),
                                 ('305', '3º andar'), ('410', '4º andar')]:
            with self.subTest(numero=numero):
                self.assertEqual(Quarto.objects.create(
                    numero=numero, bloco='B').andar, esperado)

    def test_usa_o_primeiro_digito(self):
        self.assertEqual(Quarto(numero='1', bloco='A').andar, '1º andar')
        self.assertEqual(Quarto(numero='9', bloco='A').andar, '9º andar')

    def test_ignora_prefixo_nao_numerico(self):
        self.assertEqual(Quarto(numero='A104', bloco='A').andar, '1º andar')

    def test_fallback_sem_digito(self):
        for numero in ['', 'A', 'ABC', None]:
            with self.subTest(numero=numero):
                self.assertEqual(Quarto(numero=numero, bloco='A').andar, '1º andar')

    def test_andar_nao_exige_salvamento(self):
        """E property pura: nao consulta o banco."""
        self.assertEqual(Quarto(numero='104', bloco='B').andar, '1º andar')


class VinculoNaoDuplicaQuartoTests(TestCase):
    """N estudantes no mesmo quarto 104 => 1 quarto, N moradores."""

    def setUp(self):
        u_mon = User.objects.create_user('q_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='1122335', cpf='11223344556', bloco_responsavel='B')
        self.quarto = Quarto.objects.create(numero='104', bloco='B')

    def cliente_monitor(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c

    def criar_estudante(self, username, quarto):
        u = User.objects.create_user(username, password='x')
        return Estudante.objects.create(
            user=u, matricula=username.upper() + '1', quarto=quarto)

    # ---------- property (caminho direto) ----------
    def test_dois_estudantes_no_mesmo_quarto(self):
        self.criar_estudante('aluno1', self.quarto)
        self.criar_estudante('aluno2', self.quarto)

        self.assertEqual(Quarto.objects.filter(numero='104', bloco='B').count(), 1)
        self.assertEqual(Estudante.objects.filter(quarto=self.quarto).count(), 2)

    def test_tres_estudantes_ainda_um_quarto(self):
        for i in range(3):
            self.criar_estudante('aluno%d' % i, self.quarto)
        self.assertEqual(Quarto.objects.count(), 1)
        self.assertEqual(Estudante.objects.filter(quarto=self.quarto).count(), 3)

    # ---------- pela interface (fluxo real) ----------
    def test_vincular_pelo_painel_nao_cria_quarto_novo(self):
        c = self.cliente_monitor()
        url = reverse('gestao_monitor')

        c.post(url, {
            'acao': 'novo_estudante', 'username': 'ana', 'nome': 'Ana Souza',
            'senha': 'x', 'matricula': 'A1', 'quarto': str(self.quarto.pk),
        })
        c.post(url, {
            'acao': 'novo_estudante', 'username': 'bruno', 'nome': 'Bruno Lima',
            'senha': 'x', 'matricula': 'B1', 'quarto': str(self.quarto.pk),
        })

        self.assertEqual(Quarto.objects.filter(numero='104', bloco='B').count(), 1,
                         'o cadastro criou um quarto duplicado')
        self.assertEqual(Estudante.objects.filter(quarto=self.quarto).count(), 2)

    def test_mover_estudante_nao_cria_quarto(self):
        alvo = Quarto.objects.create(numero='202', bloco='B')
        est = self.criar_estudante('carla', self.quarto)

        c = self.cliente_monitor()
        c.post(reverse('gestao_monitor'), {
            'acao': 'vincular_quarto', 'estudante': str(est.pk),
            'quarto': str(alvo.pk),
        })

        self.assertEqual(Quarto.objects.count(), 2, 'mover morador nao deve criar quarto')
        est.refresh_from_db()
        self.assertEqual(est.quarto.pk, alvo.pk)
        self.assertEqual(Estudante.objects.filter(quarto=alvo).count(), 1)

    def test_quarto_inexistente_nao_cria_estudante_orfao(self):
        c = self.cliente_monitor()
        c.post(reverse('gestao_monitor'), {
            'acao': 'novo_estudante', 'username': 'dan', 'nome': 'Dan',
            'senha': 'x', 'matricula': 'D1', 'quarto': '999999',
        })
        self.assertEqual(Quarto.objects.count(), 1, 'nada novo pode ser criado')
        self.assertFalse(User.objects.filter(username='dan').exists(),
                         'o cadastro foi recusado, mas o usuario foi criado')

    def test_quarto_id_invalido_texto_nao_quebra(self):
        c = self.cliente_monitor()
        r = c.post(reverse('gestao_monitor'), {
            'acao': 'vincular_quarto', 'estudante': '1', 'quarto': 'abc',
        })
        self.assertEqual(r.status_code, 302)   # redirect, nao 500
        self.assertEqual(Quarto.objects.count(), 1)

    # ---------- listagem sem duplicidade ----------
    def test_painel_de_vistorias_nao_duplica_estudantes(self):
        """
        Mesmo quarto, dois moradores: a sidebar mostra UM card, e ele diz a
        contagem certa. Antes eram dois cards idênticos.
        """
        self.criar_estudante('aluno1', self.quarto)
        self.criar_estudante('aluno2', self.quarto)

        c = self.cliente_monitor()
        ctx = c.get(reverse('painel_monitor')).context
        self.assertEqual(len(list(ctx['estudantes'])), 2)
        self.assertEqual(len({e.pk for e in ctx['estudantes']}), 2)

        self.assertEqual(len(ctx['quartos']), 1, 'a sidebar repetiu o quarto')
        html = c.get(reverse('painel_monitor')).content.decode()
        self.assertEqual(html.count('<a href="/quarto/%d/"' % self.quarto.pk), 1)
        self.assertIn('2 morador(es)', html)

    def test_painel_mostra_o_andar_real_do_quarto(self):
        """
        Regressão: o painel usava {{ item.andar }}, que é property de
        Quarto — num Estudante não existe, então caía no fallback e todo
        quarto aparecia como 1º andar.
        """
        from django.utils import timezone
        terceiro = Quarto.objects.create(numero='305', bloco='B')
        self.criar_estudante('aluno1', self.quarto)
        self.criar_estudante('aluno3', terceiro)

        c = self.cliente_monitor()
        html = c.get(reverse('painel_monitor')).content.decode()
        self.assertIn('1º andar', html)
        self.assertIn('3º andar', html)
        self.assertNotIn('>1º andar · 1 morador(es)</p>', html.split('305')[0][-400:])

    def test_total_quartos_na_gestao_conta_uma_vez(self):
        self.criar_estudante('aluno1', self.quarto)
        self.criar_estudante('aluno2', self.quarto)
        c = self.cliente_monitor()
        ctx = c.get(reverse('gestao_monitor')).context
        self.assertEqual(ctx['total_quartos'], 1)
        self.assertEqual(ctx['total_estudantes'], 2)


class SidebarSemDuplicacaoTests(TestCase):
    """
    Regressão do bug reportado: 1 Quarto 104 com 2 moradores aparecia DUAS
    vezes na sidebar do painel de vistorias.

    A causa NÃO era falta de `.distinct()`: a lista iterava ESTUDANTES e
    desenhava o quarto de cada um, e dois colegas são duas linhas distintas.
    A correção é a lista ser por quarto.
    """

    def setUp(self):
        u_mon = User.objects.create_user('sb_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='7654321', cpf='76543219876', bloco_responsavel='B')
        self.q = Quarto.objects.create(numero='104', bloco='B')

        self.ana = Estudante.objects.create(
            user=User.objects.create_user('ana', password='x'),
            matricula='S1', quarto=self.q)
        self.bruno = Estudante.objects.create(
            user=User.objects.create_user('bruno', password='x'),
            matricula='S2', quarto=self.q)

        from django.utils import timezone
        for nota, aprovado in [(8, True), (6, False), (9, True)]:
            Vistoria.objects.create(
                monitor=self.monitor, quarto=self.q, nota=nota,
                aprovado=aprovado, data_vistoria=timezone.localdate())

    def ctx_painel(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c.get(reverse('painel_monitor'))

    def html_painel(self):
        return self.ctx_painel().content.decode()

    # ---------- o requisito literal ----------
    def test_contexto_tem_um_unico_quarto(self):
        """1 Quarto + 2 moradores + 3 vistorias => len(quartos) == 1."""
        ctx = self.ctx_painel().context
        self.assertIn('quartos', ctx)
        self.assertEqual(len(ctx['quartos']), 1)

    def test_um_quarto_com_tres_vistorias_nao_multiplica(self):
        """As 3 vistorias nao podem virar 3 linhas de quarto."""
        self.assertEqual(len(self.ctx_painel().context['quartos']), 1)

    def test_html_mostra_o_quarto_uma_vez(self):
        html = self.html_painel()
        self.assertEqual(html.count('<a href="/quarto/%d/"' % self.q.pk), 1)
        self.assertEqual(html.count('1º andar · 2 morador(es)'), 1)

    # ---------- a causa raiz ----------
    def test_distinct_sobre_estudante_nao_resolveria(self):
        """Documenta POR QUE distinct() nao era a solucao."""
        ctx = self.ctx_painel().context
        # sao dois estudantes diferentes -> dois pks, nada a deduplicar
        self.assertEqual(len(ctx['estudantes']), 2)
        self.assertEqual(len({e.pk for e in ctx['estudantes']}), 2)

    def test_quarto_ocupado_aparece_uma_vez_com_conta_certa(self):
        quarto = list(self.ctx_painel().context['quartos'])[0]
        self.assertEqual(quarto.pk, self.q.pk)
        self.assertEqual(quarto.moradores_count, 2)

    def test_quarto_vazio_some_da_sidebar(self):
        Quarto.objects.create(numero='999', bloco='Z')   # sem moradores
        ctx = self.ctx_painel().context
        self.assertEqual(len(ctx['quartos']), 1)
        self.assertNotIn(999, [q.pk for q in ctx['quartos']])

    def test_varios_quartos_ocupados_aparecem_uma_vez_cada(self):
        outro = Quarto.objects.create(numero='202', bloco='C')
        for i in range(2):
            Estudante.objects.create(
                user=User.objects.create_user('outro%d' % i, password='x'),
                matricula='O%d' % i, quarto=outro)

        html = self.html_painel()
        self.assertEqual(html.count('/quarto/%d/' % self.q.pk), 1)
        self.assertEqual(html.count('/quarto/%d/' % outro.pk), 1)
        self.assertEqual(len(self.ctx_painel().context['quartos']), 2)

    # ---------- aba direita ----------
    def test_clique_carrega_a_aba_direita(self):
        """Clicar no quarto abre a vistoria completa daquele quarto."""
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        r = c.get(reverse('detalhes_quarto', args=[self.q.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['quarto_selecionado'], self.q)
        html = r.content.decode()
        self.assertIn('form-vistoria', html)
        self.assertIn('id="btn-aprovar"', html)
        self.assertIn('id="btn-reprovar"', html)
        self.assertIn('id="area-concluida"', html)

    def test_aba_direita_mantem_a_sidebar_sem_duplicar(self):
        """
        A página de detalhes tambem nao pode repetir o card. Contamos so os
        <a href="/quarto/N/"> da sidebar: a action do form de vistoria
        aponta para a mesma URL de proposito.
        """
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        html = c.get(reverse('detalhes_quarto', args=[self.q.pk])).content.decode()
        cards = html.count('<a href="/quarto/%d/"' % self.q.pk)
        self.assertEqual(cards, 1, 'a sidebar repetiu o card do quarto')

    def test_quarto_sem_vistoria_abre_com_area_de_botoes(self):
        vazio = Quarto.objects.create(numero='303', bloco='C')
        Estudante.objects.create(
            user=User.objects.create_user('carla', password='x'),
            matricula='C1', quarto=vazio)
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        r = c.get(reverse('detalhes_quarto', args=[vazio.pk]))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('form-vistoria', html)
        self.assertIn('id="btn-aprovar"', html)
        # O quarto aberto nao tem vistoria, mas o podio e global: o 104 do
        # setUp tem vistorias, entao o card nao pode cair no empty state.
        self.assertNotIn('Nenhum quarto disputa o pódio ainda', html)



# Tags que nao abrem/fecham - usadas pelos leitores de DOM dos testes.
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
        'link', 'meta', 'param', 'source', 'track', 'wbr'}


def filhos_diretos_de(html, alvo):
    """Tags que sao filhas DIRETAS de <alvo> no HTML renderizado."""
    from html.parser import HTMLParser

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.pilha = []
            self.filhos = []

        def handle_starttag(self, tag, attrs):
            if tag in VOID:
                return
            prof = len(self.pilha)
            if alvo in self.pilha:
                if prof - (self.pilha.index(alvo) + 1) == 0:
                    self.filhos.append((tag, dict(attrs).get('class', '')))
            self.pilha.append(tag)

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if tag in self.pilha:
                while self.pilha:
                    if self.pilha.pop() == tag:
                        break

    p = Parser()
    p.feed(html)
    return p.filhos


class EstruturaPainelMonitorTests(TestCase):
    """
    Regressão de layout: um `</div>` sobrando no meio de painel_monitor.html
    encerrava a coluna de conteúdo ANTES do <main> terminar. Tudo que vinha
    depois (painel de vistoria, gráficos, pódio, cards de ação) virava filho
    direto do `<body class="flex">`, que é row — e as seções deitavam lado a
    lado numa linha horizontal gigante.
    """

    def setUp(self):
        u_mon = User.objects.create_user('es_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='5566778', cpf='11223344556', bloco_responsavel='B')
        q = Quarto.objects.create(numero='104', bloco='B')
        Estudante.objects.create(
            user=User.objects.create_user('es_est', password='x'),
            matricula='E1', quarto=q)

    def html(self, url=None):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c.get(url or reverse('painel_monitor')).content.decode()

    # ---------- o requisito ----------
    def test_secoes_principais_sao_irmas_dentro_do_main(self):
        """
        Todas as secoes de topo precisam estar no mesmo nivel dentro do
        <main>. Com o bug, apenas a primeira chegava ate la.
        """
        filhos = filhos_diretos_de(self.html(), 'main')
        secoes = [f for f in filhos if f[0] == 'section']
        self.assertGreaterEqual(
            len(secoes), 5,
            'so %d secao(oes) chegaram ao <main> - o resto vazou para fora '
            '(1 significa o bug de fechamento)' % len(secoes))

    def test_conteudo_nao_vaza_para_o_body(self):
        """Nada de section pode virar filho direto do <body>."""
        html = self.html()
        filhos_body = filhos_diretos_de(html, 'body')
        vazados = [f for f in filhos_body if f[0] == 'section']
        self.assertEqual(
            vazados, [],
            'secoes vazaram para o <body>, que e flex-row: %s' % vazados)

    def test_main_tem_as_cinco_blocos(self):
        classes = ' | '.join(c for _, c in filhos_diretos_de(self.html(), 'main'))
        self.assertIn('lg:col-span-4', classes)    # lista de quartos
        self.assertIn('lg:col-span-8', classes)    # painel de vistoria
        self.assertIn('lg:col-span-12', classes)   # metricas/podio/acoes

    def test_nenhum_span_quebra_a_soma_da_linha(self):
        """
        As linhas somam 12 colunas: 4 (quartos) + 8 (vistoria), e as
        demais cheias. Um span sem par deixaria a linha estourar.
        """
        import re
        html = self.html()
        for linha in re.findall(r'<main[^>]*>(.*?)</main>', html, re.S):
            spans = [int(x) for x in re.findall(r'lg:col-span-(\d+)', linha)]
            # soma por "linha" da grade: tudo que for 12 fecha uma linha
            total = 0
            for s in spans:
                total += s
                if total >= 12:
                    self.assertEqual(total, 12,
                                     'linha da grade soma %d colunas, esperado 12'
                                     % total)
                    total = 0

    # ---------- as telas de verdade ----------
    def test_layout_preservado_no_painel(self):
        html = self.html()
        # sem quarto selecionado o painel direito mostra o estado vazio;
        # por isso 'Vistoria do Quarto' fica para o teste da pagina de detalhe
        for marca in ['Quartos (', 'Visão Geral do Alojamento',
                      'Pódio dos Alojamentos', 'Gerar Cronograma Semanal',
                      'Chamados de Infraestrutura']:
            self.assertIn(marca, html, 'seção ausente: %s' % marca)

    def test_layout_preservado_no_detalhe_do_quarto(self):
        quarto = Quarto.objects.first()
        html = self.html(reverse('detalhes_quarto', args=[quarto.pk]))
        for marca in ['Pódio dos Alojamentos', 'Gerar Cronograma Semanal',
                      'Visão Geral do Alojamento', 'Vistoria do Quarto']:
            self.assertIn(marca, html)

    def test_coluna_de_conteudo_encerra_no_fim(self):
        """
        O wrapper <div class="flex-1 flex flex-col min-w-0"> abre antes do
        header e tem de fechar depois do ultimo include.
        """
        html = self.html()
        self.assertLess(html.index('<header'), html.index('<main'))
        self.assertLess(html.index('<main'), html.rindex('</div>'))
        self.assertLess(html.rindex('</div>'), html.index('</body>'))

class UnificacaoQuartosTests(TestCase):
    """A sanitizacao de duplicatas (repara bases antigas)."""

    def setUp(self):
        u_mon = User.objects.create_user('un_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='1231230', cpf='12312300000', bloco_responsavel='B')

    def morador(self, username, matricula, quarto):
        return Estudante.objects.create(
            user=User.objects.create_user(username, password='x'),
            matricula=matricula, quarto=quarto)

    def test_sem_duplicatas_nao_faz_nada(self):
        Quarto.objects.create(numero='104', bloco='B')
        self.assertEqual(unificar_quartos_duplicados(), [])

    def test_base_sem_duplicatas_pelo_constraint(self):
        """A restricao impede criar a duplicata em primeiro lugar."""
        Quarto.objects.create(numero='104', bloco='B')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Quarto.objects.create(numero='104', bloco='B')

    def test_unifica_movendo_moradores(self):
        """
        Nao da para criar 104-B duplicado pelo ORM (a constraint bloqueia).
        Para exercitar o reparo, a constraint e derrubada por baixo - que e
        exatamente a base antiga que a funcao precisa consertar.
        """
        from django.db import connection

        # o nome da constraint tem sufixo de hash gerado pelo Django:
        # descobrir em vez de hardcodar
        with connection.cursor() as c:
            c.execute(
                "select con.conname from pg_constraint con "
                "join pg_class rel on rel.oid = con.conrelid "
                "where rel.relname = %s and "
                "pg_get_constraintdef(con.oid) like 'UNIQUE%%'", [Quarto._meta.db_table])
            nomes = [linha[0] for linha in c.fetchall()]
        self.assertTrue(nomes, 'constraint unica de Quarto nao encontrada')
        constraint_real = models.UniqueConstraint(fields=['numero', 'bloco'],
                                                  name=nomes[0])

        keeper = Quarto.objects.create(numero='104', bloco='B')

        # O FK Quarto.representante e criado pelo Django como DEFERRABLE.
        # O INSERT acima deixa um evento de trigger pendente na transacao do
        # TestCase, e o PostgreSQL recusa o ALTER TABLE enquanto ele existir.
        # Forcar a checagem aqui libera o ALTER sem mudar o cenario do teste.
        connection.check_constraints()

        with connection.schema_editor() as editor:
            editor.remove_constraint(Quarto, constraint_real)
        try:
            duplicado = Quarto.objects.create(numero='104', bloco='B')
            self.morador('ana', 'U1', keeper)
            self.morador('bruno', 'U2', duplicado)

            Vistoria.objects.create(monitor=self.monitor, quarto=duplicado,
                                    nota=7, aprovado=True,
                                    data_vistoria=timezone.localdate())

            relatorio = unificar_quartos_duplicados()
            self.assertEqual(len(relatorio), 1)

            self.assertEqual(Quarto.objects.count(), 1, 'a duplicata nao foi removida')
            self.assertEqual(Estudante.objects.filter(quarto=keeper).count(), 2,
                             'os moradores nao foram movidos')
            self.assertEqual(Vistoria.objects.filter(quarto=keeper).count(), 1)
        finally:
            # nao deixa a constraint quebrada se algo falhar no meio, mas nao
            # mascara o erro original com um IntegrityError do `finally`
            try:
                if not (Quarto.objects.filter(numero='104', bloco='B')
                        .values('numero').distinct().count() > 1):
                    with connection.schema_editor() as editor:
                        editor.add_constraint(Quarto, constraint_real)
            except Exception:
                pass


class RepresentanteEModalidadeTests(TestCase):
    """Representante do quarto e modalidade de divisao salvos pela Gestao."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='909', bloco='R')
        u_mon = User.objects.create_user('rep_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='9090909', cpf='90909090909', bloco_responsavel='R')
        self.representante = self.criar_estudante('rep_aluno', 'REP1', self.quarto)
        self.colega = self.criar_estudante('rep_colega', 'REP2', self.quarto)
        self.url = reverse('gestao_monitor')
        self.client.force_login(self.monitor.user)

    def criar_estudante(self, username, matricula, quarto):
        return Estudante.objects.create(
            user=User.objects.create_user(username, password='x'),
            matricula=matricula, quarto=quarto,
        )

    def test_novo_quarto_nasce_solo_sem_representante(self):
        self.assertIsNone(self.quarto.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_SOLO)

    def test_post_define_representante_e_modo(self):
        r = self.client.post(self.url, {
            'acao': 'configurar_quarto',
            'quarto': str(self.quarto.pk),
            'representante': str(self.representante.pk),
            'modo_divisao': Quarto.MODO_DUPLA,
        })
        self.assertEqual(r.status_code, 302)
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.representante, self.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_DUPLA)

    def test_post_remove_representante(self):
        self.quarto.representante = self.representante
        self.quarto.save(update_fields=['representante'])

        self.client.post(self.url, {
            'acao': 'configurar_quarto',
            'quarto': str(self.quarto.pk),
            'representante': '',
            'modo_divisao': Quarto.MODO_LIVRE,
        })
        self.quarto.refresh_from_db()
        self.assertIsNone(self.quarto.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_LIVRE)

    def test_aluno_nao_configura_representante_nem_modo_do_proprio_quarto(self):
        aluno = Client(SERVER_NAME='localhost')
        aluno.force_login(self.colega.user)

        r = aluno.post(reverse('painel_estudante'), {
            'acao': 'configurar_quarto',
            'representante': str(self.representante.pk),
            'modo_divisao': Quarto.MODO_DUPLA,
        })

        self.assertEqual(r.status_code, 403)
        self.quarto.refresh_from_db()
        self.assertIsNone(self.quarto.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_SOLO)

    def test_aluno_nao_consegue_apontar_representante_de_outro_quarto(self):
        outro = Quarto.objects.create(numero='911', bloco='R')
        intruso = self.criar_estudante('rep_aluno_intruso', 'REP4', outro)
        aluno = Client(SERVER_NAME='localhost')
        aluno.force_login(self.colega.user)

        r = aluno.post(reverse('painel_estudante'), {
            'acao': 'configurar_quarto',
            'representante': str(intruso.pk),
            'modo_divisao': Quarto.MODO_DUPLA,
        })

        self.assertEqual(r.status_code, 403)
        self.quarto.refresh_from_db()
        self.assertIsNone(self.quarto.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_SOLO)


    def test_representante_de_outro_quarto_e_rejeitado(self):
        outro = Quarto.objects.create(numero='910', bloco='R')
        intruso = self.criar_estudante('rep_intruso', 'REP3', outro)

        self.client.post(self.url, {
            'acao': 'configurar_quarto',
            'quarto': str(self.quarto.pk),
            'representante': str(intruso.pk),
            'modo_divisao': Quarto.MODO_DUPLA,
        })
        self.quarto.refresh_from_db()
        self.assertIsNone(self.quarto.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_SOLO)

    def test_modalidade_invalida_nao_altera_nada(self):
        self.client.post(self.url, {
            'acao': 'configurar_quarto',
            'quarto': str(self.quarto.pk),
            'representante': str(self.representante.pk),
            'modo_divisao': 'INVALIDA',
        })
        self.quarto.refresh_from_db()
        self.assertIsNone(self.quarto.representante)
        self.assertEqual(self.quarto.modo_divisao, Quarto.MODO_SOLO)

    def test_gestao_expoe_seletor_tag_e_badge(self):
        self.quarto.representante = self.representante
        self.quarto.modo_divisao = Quarto.MODO_DUPLA
        self.quarto.save(update_fields=['representante', 'modo_divisao'])

        html = self.client.get(self.url).content.decode()
        self.assertIn('Representante e modalidade', html)
        self.assertIn('name="representante"', html)
        self.assertIn('name="modo_divisao"', html)
        self.assertIn('&#11088;', html)
        self.assertIn('Dupla', html)

    def test_painel_do_estudante_expoe_badge_e_tag_da_modalidade(self):
        self.quarto.representante = self.representante
        self.quarto.modo_divisao = Quarto.MODO_DUPLA
        self.quarto.save(update_fields=['representante', 'modo_divisao'])

        aluno = Client(SERVER_NAME='localhost')
        aluno.force_login(self.representante.user)
        html = aluno.get(reverse('painel_estudante')).content.decode()
        self.assertIn('Voc\u00ea \u00e9 o Representante do Quarto', html)
        self.assertIn('Divis\u00e3o Dupla', html)
        # O representante so altera a modalidade; o representante e do monitor.
        self.assertNotIn('name="representante"', html)
        self.assertIn('name="modo_divisao"', html)
        self.assertIn('Salvar Modalidade', html)
        self.assertIn(str(self.colega), html)

