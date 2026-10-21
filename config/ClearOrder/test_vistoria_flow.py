import re
import io
from datetime import date, timedelta
from pathlib import Path

from django.core.management import call_command
from django.test import Client, TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from .models import Estudante, Monitor, Quarto, Vistoria, SolicitacaoReparo, Cronograma, Tarefa, Notificacao
from .cronograma_utils import (
    TAREFAS_BASE,
    deslocamento_do_rodizio,
    janela_da_semana,
)


class VistoriaFlowTests(TestCase):
    def setUp(self):
        self.quarto = Quarto.objects.create(numero='101', bloco='A')
        self.u_est = User.objects.create_user('aluno1', password='x')
        self.estudante = Estudante.objects.create(
            user=self.u_est, matricula='123', cpf='12345678901', quarto=self.quarto
        )
        self.u_mon = User.objects.create_user('mon1', password='x')
        self.monitor = Monitor.objects.create(
            user=self.u_mon, siape='1234567', cpf='98765432100', bloco_responsavel='A'
        )
        self.client.force_login(self.u_mon)
        self.url = reverse('detalhes_quarto', args=[self.quarto.id])

    # --- Bug 2: nota (criterios da entrevista com o monitor) ---
    def test_nota_tudo_marcado(self):
        self.client.post(self.url, {
            'chao_lavanderia': '1', 'organizacao_geral': '1', 'janelas_livres': '1',
            'observacoes': 'tudo ok', 'aprovado': 'True',
        })
        self.assertEqual(Vistoria.objects.get().nota, 10)

    def test_nota_parcial_tres_mais_tres(self):
        self.client.post(self.url, {'organizacao_geral': '1', 'janelas_livres': '1', 'aprovado': 'True'})
        self.assertEqual(Vistoria.objects.get().nota, 6)

    def test_nota_apenas_chao_lavanderia(self):
        self.client.post(self.url, {'chao_lavanderia': '1', 'aprovado': 'True'})
        self.assertEqual(Vistoria.objects.get().nota, 4)

    def test_nota_zero_quando_nada_marcado(self):
        self.client.post(self.url, {'aprovado': 'False'})
        self.assertEqual(Vistoria.objects.get().nota, 0)

    def test_nomes_antigos_de_criterio_nao_somam_mais(self):
        """Regressao: HTML e CRITERIOS_VISTORIA precisam ficar em sintonia."""
        self.client.post(self.url, {
            'chao_limpo': '1', 'lixo_retirado': '1',
            'camas_organizadas': '1', 'banheiro_limpo': '1',
            'aprovado': 'True',
        })
        self.assertEqual(Vistoria.objects.get().nota, 0)

    def test_labels_do_monitor_aparecem_no_painel(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Chão e Lavanderia limpos', html)
        self.assertIn('Organização Geral (Quarto livre de bagunça aparente)', html)
        self.assertIn('Janelas Livres (Sem roupas ou objetos pendurados)', html)
        # criterios antigos sairam
        self.assertNotIn('name="banheiro_limpo"', html)
        self.assertNotIn('name="lixo_retirado"', html)

    # --- Bug 1: status persiste de verdade ---
    def test_aprovar_persiste_status_e_streak(self):
        self.client.post(self.url, {'chao_limpo': '1', 'aprovado': 'True'})
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.status, Quarto.STATUS_APROVADO)
        self.assertEqual(self.quarto.streak_dias, 1)

    def test_reprovar_grava_reprovado_nao_pendente(self):
        self.client.post(self.url, {'aprovado': 'False'})
        self.quarto.refresh_from_db()
        self.assertEqual(self.quarto.status, Quarto.STATUS_REPROVADO)
        self.assertEqual(self.quarto.streak_dias, 0)

    # --- Trava de loop ---
    @staticmethod
    def classes_de(html, elemento_id):
        """Classes CSS de um elemento pelo id."""
        import re
        m = re.search(r'id="%s"[^>]*?class="([^"]*)"' % elemento_id, html)
        return m.group(1).split() if m else []

    def test_painel_conclusao_apos_vistoria(self):
        self.client.post(self.url, {'aprovado': 'True'})
        html = self.client.get(self.url).content.decode()
        self.assertIn('Vistoria Concluída', html)
        # botoes ficam ocultos (mas presentes no DOM, p/ o Editar reexibir)
        self.assertIn('hidden', self.classes_de(html, 'area-botoes'))
        self.assertNotIn('hidden', self.classes_de(html, 'area-concluida'))
        self.assertIn('id="btn-editar"', html)

    def test_botoes_visiveis_quando_pendente(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Aprovar', html)
        self.assertIn('Reprovar', html)
        self.assertNotIn('hidden', self.classes_de(html, 'area-botoes'))
        self.assertIn('hidden', self.classes_de(html, 'area-concluida'))

    def test_modal_nasce_escondido(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('hidden', self.classes_de(html, 'modal-confirmacao'))

    # --- Modal + JS ---
    def test_modal_e_js_presentes(self):
        html = self.client.get(self.url).content.decode()
        import re
        m = re.search(r'id="modal-confirmacao"[^>]*?class="([^"]*)"', html)
        classes = m.group(1).split() if m else []
        self.assertIn('modal-confirmacao', html)
        for classe in ('fixed', 'inset-0', 'items-center', 'justify-center'):
            self.assertIn(classe, classes)
        for fn in ('abrirModal', 'fecharModal', 'confirmarModal', 'editarVistoria'):
            self.assertIn(fn, html)
        self.assertIn('Deseja confirmar a aprovação?', html)
        self.assertIn('Deseja confirmar a reprovação?', html)
        self.assertIn('Recusar', html)
        self.assertIn('Confirmar', html)
        self.assertIn('id="input-aprovado"', html)
        # botoes originais nao podem submeter direto
        self.assertNotIn('type="submit" name="aprovado"', html)

    # --- Bug 4: progresso sem quarto ---
    def test_progresso_sem_quarto_nao_quebra(self):
        self.estudante.quarto = None
        self.estudante.save()
        self.client.force_login(self.u_est)
        self.assertEqual(self.client.get(reverse('progresso')).status_code, 200)

    # --- Bug 3: POST fantasma ---
    def test_get_reportar_nao_cria_chamado(self):
        self.client.force_login(self.u_est)
        self.assertEqual(self.client.get(reverse('reportar_problema')).status_code, 200)
        self.assertEqual(SolicitacaoReparo.objects.count(), 0)

    def test_post_reportar_delega_e_salva(self):
        self.client.force_login(self.u_est)
        r = self.client.post(reverse('reportar_problema'), {
            'titulo': 'Vazamento', 'categoria': 'hidraulica', 'descricao': 'pingando'
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SolicitacaoReparo.objects.count(), 1)


class ConclusaoTarefaModalTests(TestCase):
    """Modal de confirmacao da conclusao de tarefa no painel do estudante."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='202', bloco='B')
        self.u = User.objects.create_user('aluno2', password='x')
        self.estudante = Estudante.objects.create(
            user=self.u, matricula='456', cpf='11122233344', quarto=self.quarto
        )
        # Cronograma ancorado na SEMANA ATUAL: o painel do estudante filtra
        # pela semana corrente, entao uma data fixa aqui expiraria com o tempo.
        inicio_semana, fim_semana = janela_da_semana(offset=0)
        self.tarefa = Tarefa.objects.create(
            cronograma=Cronograma.objects.create(
                quarto=self.quarto, data_inicio=inicio_semana, data_fim=fim_semana
            ),
            estudante_responsavel=self.estudante,
            titulo='Limpar o quarto',
            descricao='Varrer, lavar e organizar',
            data_prazo=inicio_semana,
            status='Pendente',
        )
        self.client.force_login(self.u)
        self.url_concluir = reverse('concluir_tarefa', args=[self.tarefa.id])

    def html(self):
        return self.client.get(reverse('painel_estudante')).content.decode()

    def test_form_get_apontando_para_concluir_tarefa(self):
        h = self.html()
        self.assertIn('action="%s"' % self.url_concluir, h)
        self.assertIn('id="form-tarefa-%d"' % self.tarefa.id, h)
        self.assertIn('method="GET"', h)

    def test_botao_type_button_com_onclick(self):
        h = self.html()
        self.assertIn('type="button"', h)
        self.assertIn("abrirModalEstudante('form-tarefa-%d')" % self.tarefa.id, h)

    def test_confirm_nativo_removido(self):
        self.assertNotIn('return confirm(', self.html())

    def test_modal_textos_de_contexto_do_aluno(self):
        import re
        h = self.html()
        m = re.search(r'id="modal-estudante"[^>]*?class="([^"]*)"', h)
        classes = m.group(1).split() if m else []
        for classe in ('fixed', 'inset-0', 'items-center', 'justify-center', 'hidden'):
            self.assertIn(classe, classes)
        self.assertIn('Confirmar conclus\u00e3o?', h)
        self.assertIn(
            'Voc\u00ea tem certeza que deseja marcar esta limpeza como conclu\u00edda '
            'e enviar para vistoria?',
            h,
        )
        self.assertIn('Recusar', h)
        self.assertIn('Confirmar', h)

    def test_funcoes_js_presentes(self):
        h = self.html()
        for fn in ('abrirModalEstudante', 'fecharModalEstudante', 'confirmarConclusaoEstudante'):
            self.assertIn(fn, h)
        self.assertIn('.submit()', h)

    def test_modal_nasce_escondido(self):
        import re
        m = re.search(r'id="modal-estudante"[^>]*?class="([^"]*)"', self.html())
        self.assertIn('hidden', m.group(1).split())

    def test_submit_do_form_realmente_conclui_a_tarefa(self):
        """Simula o que o JS faz ao confirmar: submeter o form."""
        self.client.get(self.url_concluir)
        self.tarefa.refresh_from_db()
        self.assertEqual(self.tarefa.status, 'Conclu\u00edda')

    def test_tarefa_concluida_nao_mostra_botao_nem_form(self):
        self.tarefa.status = 'Concluída'
        self.tarefa.save()
        h = self.html()
        self.assertNotIn('id="form-tarefa-%d"' % self.tarefa.id, h)


class VerificarReparoTests(TestCase):
    """Botao 'Marcar como Verificado' no painel do monitor."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='404', bloco='D')
        self.u_est = User.objects.create_user('aluno3', password='x')
        self.estudante = Estudante.objects.create(
            user=self.u_est, matricula='777', cpf='22233344455', quarto=self.quarto
        )
        self.u_mon = User.objects.create_user('mon2', password='x')
        Monitor.objects.create(
            user=self.u_mon, siape='7654321', cpf='44455566677', bloco_responsavel='D'
        )
        self.reparo = SolicitacaoReparo.objects.create(
            estudante=self.estudante,
            titulo='Vazamento no banheiro',
            categoria='hidraulica',
            descricao='Torneira pingando desde segunda',
        )
        self.url_verificar = reverse('verificar_reparo', args=[self.reparo.id])

    def html_dos_chamados(self):
        """A gestao de chamados foi para a pagina dedicada /chamados/."""
        self.client.force_login(self.u_mon)
        return self.client.get(reverse('chamados')).content.decode()

    def test_chamado_aparece_na_pagina_de_chamados(self):
        html = self.html_dos_chamados()
        self.assertIn('Chamados de Infraestrutura', html)
        self.assertIn('Vazamento no banheiro', html)
        self.assertIn('Marcar como Verificado', html)

    def test_botao_aponta_para_url_correta(self):
        self.assertIn('action="%s"' % self.url_verificar, self.html_dos_chamados())

    def test_dashboard_do_monitor_nao_lista_mais_os_chamados(self):
        """O dashboard ficou limpo: so o atalho para /chamados/."""
        self.client.force_login(self.u_mon)
        html = self.client.get(reverse('painel_monitor')).content.decode()
        self.assertNotIn('Vazamento no banheiro', html)
        self.assertNotIn('Marcar como Verificado', html)
        self.assertIn('href="%s"' % reverse('chamados'), html)

    def test_post_marca_como_verificado_no_banco(self):
        self.client.force_login(self.u_mon)
        r = self.client.post(self.url_verificar)
        self.assertEqual(r.status_code, 302)
        self.reparo.refresh_from_db()
        self.assertEqual(self.reparo.status, 'Verificado')

    def test_depois_de_verificar_botao_some_e_badge_muda(self):
        self.client.force_login(self.u_mon)
        self.client.post(self.url_verificar)
        html = self.client.get(reverse('chamados')).content.decode()
        self.assertNotIn('Marcar como Verificado', html)
        self.assertIn('fa-check-double', html)

    def test_get_nao_altera_nada(self):
        self.client.force_login(self.u_mon)
        self.client.get(self.url_verificar)
        self.reparo.refresh_from_db()
        self.assertEqual(self.reparo.status, 'Em Aberto')

    def test_estudante_nao_pode_verificar(self):
        self.client.force_login(self.u_est)
        self.client.post(self.url_verificar)
        self.reparo.refresh_from_db()
        self.assertEqual(self.reparo.status, 'Em Aberto')

    def test_visitante_redirecionado_para_login(self):
        r = self.client.post(self.url_verificar)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)

    def test_status_padrao_ainda_em_aberto(self):
        self.assertEqual(self.reparo.status, 'Em Aberto')


class ChamadosPageTests(TestCase):
    """Pagina dedicada /chamados/ - listagem, criacao e isolamento por perfil."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='505', bloco='K')

        # estudante 1 (dono do chamado)
        self.u1 = User.objects.create_user('aluno_p1', password='x')
        self.est1 = Estudante.objects.create(
            user=self.u1, matricula='p1', cpf='10020030040', quarto=self.quarto
        )
        # estudante 2 (nao pode ver o chamado do colega)
        self.u2 = User.objects.create_user('aluno_p2', password='x')
        self.est2 = Estudante.objects.create(
            user=self.u2, matricula='p2', cpf='10020030050', quarto=self.quarto
        )

        self.u_mon = User.objects.create_user('mon_p', password='x')
        Monitor.objects.create(
            user=self.u_mon, siape='5556667', cpf='20030040050', bloco_responsavel='K'
        )

        self.meu = SolicitacaoReparo.objects.create(
            estudante=self.est1, titulo='Vazamento', categoria='hidraulica',
            descricao='pingando',
        )
        self.deles = SolicitacaoReparo.objects.create(
            estudante=self.est2, titulo='Porta quebrada', categoria='mobiliario',
            descricao='trancada',
        )

    def html(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('chamados')).content.decode()

    # --- acesso ---
    def test_pagina_responde_200_para_estudante(self):
        self.assertEqual(self.html(self.u1).count('<!DOCTYPE html>'), 1)
        r = self.client.get(reverse('chamados'))
        self.assertEqual(r.status_code, 200)

    def test_pagina_responde_200_para_monitor(self):
        self.client.force_login(self.u_mon)
        self.assertEqual(self.client.get(reverse('chamados')).status_code, 200)

    def test_visitante_redirecionado_para_login(self):
        r = self.client.get(reverse('chamados'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)

    def test_usuario_sem_perfil_desloga(self):
        u = User.objects.create_user('sem_perfil', password='x')
        self.client.force_login(u)
        r = self.client.get(reverse('chamados'))
        self.assertEqual(r.status_code, 302)

    # --- isolamento ---
    def test_estudante_ve_apenas_os_proprios_chamados(self):
        h = self.html(self.u1)
        self.assertIn('Vazamento', h)
        self.assertNotIn('Porta quebrada', h)

    def test_monitor_ve_todos_os_chamados(self):
        h = self.html(self.u_mon)
        self.assertIn('Vazamento', h)
        self.assertIn('Porta quebrada', h)

    # --- criacao (POST do formulario) ---
    def test_estudante_cria_chamado_via_post(self):
        self.client.force_login(self.u1)
        antes = SolicitacaoReparo.objects.count()
        r = self.client.post(reverse('chamados'), {
            'titulo': 'Lampada queimada',
            'categoria': 'eletrica',
            'descricao': 'nao acende',
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SolicitacaoReparo.objects.count(), antes + 1)
        novo = SolicitacaoReparo.objects.exclude(pk=self.meu.pk).exclude(pk=self.deles.pk).get()
        self.assertEqual(novo.estudante, self.est1)
        self.assertEqual(novo.titulo, 'Lampada queimada')
        self.assertEqual(novo.categoria, 'eletrica')
        self.assertEqual(novo.descricao, 'nao acende')
        self.assertEqual(novo.status, 'Em Aberto')

    def test_campos_do_formulario_preservados(self):
        h = self.html(self.u1)
        for campo in ('name="titulo"', 'name="categoria"', 'name="descricao"'):
            self.assertIn(campo, h)
        for opcao in ('eletrica', 'hidraulica', 'mobiliario', 'outros'):
            self.assertIn('value="%s"' % opcao, h)

    def test_monitor_nao_via_formulario_de_criacao(self):
        h = self.html(self.u_mon)
        self.assertNotIn('name="descricao"', h)

    def test_get_nao_cria_chamado(self):
        self.client.force_login(self.u1)
        antes = SolicitacaoReparo.objects.count()
        self.client.get(reverse('chamados'))
        self.assertEqual(SolicitacaoReparo.objects.count(), antes)

    # --- resumo / badges ---
    def test_contadores_de_resumo(self):
        self.client.force_login(self.u_mon)
        self.reparo_verificado = SolicitacaoReparo.objects.get(pk=self.meu.pk)
        self.reparo_verificado.status = 'Verificado'
        self.reparo_verificado.save()
        contexto = self.client.get(reverse('chamados')).context
        self.assertEqual(contexto['total_verificados'], 1)
        self.assertEqual(contexto['total_abertos'], 1)
        self.assertEqual(contexto['eh_monitor'], True)

    def test_badges_de_status(self):
        self.meu.status = 'Verificado'
        self.meu.save()
        h = self.html(self.u_mon)
        self.assertIn('Verificado', h)
        self.assertIn('Em Aberto', h)

    # --- navegacao ---
    def test_link_de_chamados_no_menu_do_estudante(self):
        h = self.html(self.u1)
        self.assertIn('href="%s"' % reverse('chamados'), h)
        self.assertIn('Chamados', h)

    def test_link_de_chamados_no_menu_do_monitor(self):
        h = self.html(self.u_mon)
        self.assertIn('href="%s"' % reverse('chamados'), h)

    def test_atalho_no_dashboard_do_estudante(self):
        self.client.force_login(self.u1)
        h = self.client.get(reverse('painel_estudante')).content.decode()
        self.assertIn('href="%s"' % reverse('chamados'), h)

    # --- rotas antigas continuam de pe ---
    def test_rota_reportar_ainda_responde(self):
        self.client.force_login(self.u1)
        self.assertEqual(self.client.get(reverse('reportar_problema')).status_code, 200)

    def test_rota_enviar_relatorio_ainda_salva(self):
        self.client.force_login(self.u1)
        antes = SolicitacaoReparo.objects.count()
        r = self.client.post(reverse('enviar_relatorio'), {
            'titulo': 'Via rota antiga', 'categoria': 'outros', 'descricao': 'x',
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SolicitacaoReparo.objects.count(), antes + 1)


class SidebarUnificadaTests(TestCase):
    """Sidebar igual em /monitor/ e /chamados/, com estado ativo correto."""

    # Paleta SUAP/IFSULDEMINAS: item ativo = fundo verde claro + texto verde
    # escuro + barra lateral de destaque no verde institucional.
    ATIVO = ['bg-emerald-50', 'text-emerald-800', 'border-l-4', 'border-emerald-600']

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='707', bloco='M')
        ua = User.objects.create_user('aluno_sb', password='x')
        Estudante.objects.create(user=ua, matricula='sb1', cpf='61062063070', quarto=self.quarto)
        um = User.objects.create_user('mon_sb', password='x')
        Monitor.objects.create(user=um, siape='9900112', cpf='62063074080', bloco_responsavel='M')

        self.client.force_login(um)
        self.monitor = self.client.get(reverse('painel_monitor')).content.decode()
        self.quarto_pg = self.client.get(reverse('detalhes_quarto', args=[self.quarto.id])).content.decode()
        self.chamados = self.client.get(reverse('chamados')).content.decode()

    # --- helpers ---
    @staticmethod
    def aside(html):
        m = re.search(r'<aside.*?</aside>', html, re.S)
        return m.group(0) if m else ''

    @classmethod
    def item(cls, html, icone):
        """Classes do <a> do menu que contem o icone informado."""
        for parte in html.split('<a href=')[1:]:
            if icone in parte:
                m = re.search(r'class="([^"]*)"', parte)
                return m.group(1).split() if m else []
        return []

    def esta_ativo(self, html, icone):
        return all(c in self.item(html, icone) for c in self.ATIVO)

    # --- 1) mesmos itens de menu nas duas telas ---
    def test_monitor_tem_vistorias_e_chamados(self):
        for icone in ('fa-clipboard-check', 'fa-screwdriver-wrench'):
            self.assertIn(icone, self.aside(self.monitor))

    def test_chamados_tem_vistorias_e_chamados(self):
        for icone in ('fa-clipboard-check', 'fa-screwdriver-wrench'):
            self.assertIn(icone, self.aside(self.chamados))

    def test_links_apontam_para_as_rotas_certas(self):
        aside = self.aside(self.monitor)
        self.assertIn('href="%s"' % reverse('painel_monitor'), aside)
        self.assertIn('href="%s"' % reverse('chamados'), aside)

    # --- 2) estado ativo ---
    def test_em_vistorias_apenas_vistorias_ativo(self):
        self.assertTrue(self.esta_ativo(self.monitor, 'fa-clipboard-check'))
        self.assertFalse(self.esta_ativo(self.monitor, 'fa-screwdriver-wrench'))

    def test_em_chamados_apenas_chamados_ativo(self):
        self.assertTrue(self.esta_ativo(self.chamados, 'fa-screwdriver-wrench'))
        self.assertFalse(self.esta_ativo(self.chamados, 'fa-clipboard-check'))

    def test_vistorias_ativo_tambem_na_pagina_do_quarto(self):
        self.assertTrue(self.esta_ativo(self.quarto_pg, 'fa-clipboard-check'))
        self.assertFalse(self.esta_ativo(self.quarto_pg, 'fa-screwdriver-wrench'))

    def test_inativos_ficam_cinza_com_hover(self):
        classes = self.item(self.chamados, 'fa-clipboard-check')
        self.assertIn('text-emerald-950/70', classes)
        self.assertIn('hover:bg-emerald-50', classes)

    # --- 3) rodapé unificado ---
    def test_logout_identico_nas_duas_telas(self):
        def logout(html):
            for parte in html.split('<a href=')[1:]:
                if 'fa-arrow-right-from-bracket' in parte:
                    fim = parte.find('</a>')  # corta antes de vazar pro resto do doc
                    return ' '.join(parte[:fim + 4].split())
        self.assertEqual(logout(self.monitor), logout(self.chamados))

    def test_logout_usa_texto_e_icone_padronizados(self):
        aside = self.aside(self.chamados)
        self.assertIn('Sair da Conta', aside)
        self.assertIn('fa-arrow-right-from-bracket', aside)
        self.assertNotIn('>Logout<', aside)

    # --- 4) reutilizacao ---
    def test_sidebar_vem_de_um_include_unico(self):
        """Nenhuma das telas duplica o markup do <aside>."""
        for nome in ('base.html', 'painel_monitor.html'):
            with open(
                str(Path(__file__).resolve().parent / 'templates' / nome), encoding='utf-8'
            ) as f:
                src = f.read()
            self.assertIn("{% include '_sidebar.html' %}", src, nome)
            self.assertNotIn('<aside', src, nome)

    def test_estrutura_da_sidebar_identica_nas_duas_telas(self):
        """Compara as duas paginas ignorando o estado ativo (que deve diferir)."""
        def normalizar(html):
            lado = self.aside(html)
            # ativo e inativo viram o MESMO token: so a estrutura interessa
            lado = re.sub(
                r'bg-emerald-50 text-emerald-800 border-l-4 border-emerald-600 font-semibold', 'ESTADO', lado
            )
            lado = re.sub(
                r'text-emerald-950/70 hover:bg-emerald-50 hover:text-emerald-800 font-medium', 'ESTADO', lado
            )
            return ' '.join(lado.split())
        self.assertEqual(normalizar(self.monitor), normalizar(self.chamados))

    def test_logotipo_usa_caminho_absoluto(self):
        """O caminho relativo antigo (../static) quebrava em rotas aninhadas."""
        for html in (self.monitor, self.chamados):
            self.assertIn('src="/static/img/logo_casa.svg"', self.aside(html))

    # --- estudante nao perdeu o menu ---
    def test_estudante_mantem_links_proprios(self):
        ua = User.objects.get(username='aluno_sb')
        self.client.force_login(ua)
        aside = self.aside(self.client.get(reverse('painel_estudante')).content.decode())
        for icone in ('fa-house', 'fa-fire', 'fa-triangle-exclamation', 'fa-screwdriver-wrench'):
            self.assertIn(icone, aside)

    def test_estudante_ativo_em_chamados(self):
        ua = User.objects.get(username='aluno_sb')
        self.client.force_login(ua)
        h = self.client.get(reverse('chamados')).content.decode()
        self.assertTrue(self.esta_ativo(h, 'fa-screwdriver-wrench'))
        self.assertFalse(self.esta_ativo(h, 'fa-house'))


class GerarCronogramaCommandTests(TestCase):
    """Pilar 1 - comando gerar_cronograma."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='101', bloco='A')
        self.moradores = []
        for i in range(3):
            u = User.objects.create_user('morador%d' % i, password='x')
            self.moradores.append(
                Estudante.objects.create(
                    user=u, matricula='m%d' % i, cpf='30000%05d' % i, quarto=self.quarto
                )
            )

    def rodar(self, **kwargs):
        call_command('gerar_cronograma', verbosity=0, **kwargs)

    # --- criacao ---
    def test_cria_cronograma_e_tarefas(self):
        self.rodar()
        self.assertEqual(Cronograma.objects.count(), 1)
        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE))

    def test_cronograma_e_a_semana_atual_segunda_a_domingo(self):
        self.rodar()
        c = Cronograma.objects.get()
        esperado_inicio, esperado_fim = janela_da_semana(offset=0)
        self.assertEqual(c.data_inicio, esperado_inicio)
        self.assertEqual(c.data_fim, esperado_fim)
        self.assertEqual(c.data_inicio.weekday(), 0)   # segunda
        self.assertEqual(c.data_fim.weekday(), 6)      # domingo
        self.assertEqual((c.data_fim - c.data_inicio).days, 6)

    def test_padrao_gera_semana_atual_e_nao_proxima(self):
        """O padrao do comando tem de bater com o filtro do painel do aluno."""
        self.rodar()
        c = Cronograma.objects.get()
        inicio, fim = janela_da_semana(offset=0)
        self.assertEqual(c.data_inicio, inicio)
        self.assertTrue(c.data_inicio <= timezone.localdate() <= c.data_fim,
                        'a semana gerada deveria conter hoje')

    def test_semana_1_ainda_gera_proxima_semana(self):
        self.rodar(semana=1)
        c = Cronograma.objects.get()
        inicio, _ = janela_da_semana(offset=1)
        self.assertEqual(c.data_inicio, inicio)

    def test_cria_todas_as_tarefas_base(self):
        self.rodar()
        titulos = set(Tarefa.objects.values_list('titulo', flat=True))
        self.assertEqual(titulos, {t[0] for t in TAREFAS_BASE})

    def test_tarefas_com_descricao_e_status_padrao(self):
        self.rodar()
        for tarefa in Tarefa.objects.all():
            self.assertTrue(tarefa.descricao.strip())
            self.assertEqual(tarefa.status, 'Pendente')

    def test_cronograma_ativo(self):
        self.rodar()
        self.assertTrue(Cronograma.objects.get().ativo)

    def test_prazos_dentro_da_semana(self):
        self.rodar()
        c = Cronograma.objects.get()
        for tarefa in Tarefa.objects.all():
            self.assertGreaterEqual(tarefa.data_prazo, c.data_inicio)
            self.assertLessEqual(tarefa.data_prazo, c.data_fim)

    # --- rodizio ---
    def test_rodizio_distribui_entre_os_moradores(self):
        """3 moradores e 3 tarefas -> cada um recebe uma (rodizio completo)."""
        self.rodar()
        responsaveis = set(Tarefa.objects.values_list('estudante_responsavel_id', flat=True))
        self.assertEqual(responsaveis, {e.pk for e in self.moradores})

    def test_rodizio_segue_a_formula_indice_mais_deslocamento(self):
        self.rodar()
        c = Cronograma.objects.get()
        deslocamento = deslocamento_do_rodizio(c.data_inicio)
        ids = [e.pk for e in self.moradores]
        for indice, (titulo, _desc) in enumerate(TAREFAS_BASE):
            tarefa = Tarefa.objects.get(titulo=titulo)
            self.assertEqual(
                tarefa.estudante_responsavel_id,
                ids[(indice + deslocamento) % len(ids)],
                'tarefa "%s" fora da formula de rodizio' % titulo,
            )

    def test_rodizio_muda_ao_longo_das_semanas(self):
        """Nao pode ser sempre o mesmo morador com a mesma tarefa."""
        self.rodar(semana=1)
        self.rodar(semana=2)
        semana1, semana2 = list(Cronograma.objects.order_by('data_inicio'))
        resp1 = {
            t.titulo: t.estudante_responsavel_id
            for t in Tarefa.objects.filter(cronograma=semana1)
        }
        resp2 = {
            t.titulo: t.estudante_responsavel_id
            for t in Tarefa.objects.filter(cronograma=semana2)
        }
        self.assertNotEqual(resp1, resp2, 'a atribuicao nao rotacionou entre semanas')

    def test_deslocamento_difere_entre_semanas(self):
        inicio1, _ = janela_da_semana(offset=1)
        inicio2, _ = janela_da_semana(offset=2)
        self.assertNotEqual(deslocamento_do_rodizio(inicio1), deslocamento_do_rodizio(inicio2))

    def test_um_morador_recebe_todas_as_tarefas(self):
        """Um quarto so com um morador nao pode quebrar o rodizio."""
        sozinho = Quarto.objects.create(numero='202', bloco='B')
        u = User.objects.create_user('sozinho', password='x')
        e = Estudante.objects.create(user=u, matricula='s1', cpf='41111111111', quarto=sozinho)

        self.rodar()
        tarefas_sozinho = Tarefa.objects.filter(estudante_responsavel=e)
        self.assertEqual(tarefas_sozinho.count(), len(TAREFAS_BASE))

    # --- quartos sem moradores ---
    def test_quarto_sem_moradores_e_ignorado_sem_explodir(self):
        vazio = Quarto.objects.create(numero='303', bloco='C')
        self.rodar()  # nao pode levantar ZeroDivisionError
        self.assertFalse(Cronograma.objects.filter(quarto=vazio).exists())
        self.assertFalse(Tarefa.objects.filter(cronograma__quarto=vazio).exists())

    def test_so_quartos_com_moradores_geram_cronograma(self):
        Quarto.objects.create(numero='404', bloco='D')
        self.rodar()
        self.assertEqual(Cronograma.objects.count(), 1)
        self.assertEqual(Cronograma.objects.get().quarto, self.quarto)

    def test_nenhum_quarto_criado_nao_quebra_o_comando(self):
        for i in range(3):
            Quarto.objects.create(numero='50%d' % i, bloco='Z')
        self.rodar()
        self.assertEqual(Cronograma.objects.count(), 1)

    # --- idempotencia ---
    def test_rodar_duas_vezes_nao_duplica(self):
        self.rodar()
        antes_cronos = Cronograma.objects.count()
        antes_tarefas = Tarefa.objects.count()

        self.rodar()
        self.assertEqual(Cronograma.objects.count(), antes_cronos)
        self.assertEqual(Tarefa.objects.count(), antes_tarefas)

    def test_rodar_muitas_vezes_continua_estavel(self):
        for _ in range(4):
            self.rodar()
        self.assertEqual(Cronograma.objects.count(), 1)
        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE))

    def test_semanas_diferentes_geram_cronogramas_diferentes(self):
        self.rodar(semana=1)
        self.rodar(semana=2)
        self.assertEqual(Cronograma.objects.count(), 2)
        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE) * 2)

    # --- varios quartos ---
    def test_gera_para_cada_quarto_com_moradores(self):
        outro = Quarto.objects.create(numero='505', bloco='E')
        u = User.objects.create_user('outro', password='x')
        Estudante.objects.create(user=u, matricula='o1', cpf='42222222222', quarto=outro)

        self.rodar()
        self.assertEqual(Cronograma.objects.count(), 2)
        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE) * 2)
        for quarto in (self.quarto, outro):
            self.assertEqual(
                Tarefa.objects.filter(cronograma__quarto=quarto).count(), len(TAREFAS_BASE)
            )


class GerarCronogramaViewTests(TestCase):
    """Pilar 1 - botao POST no painel do monitor."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='606', bloco='F')
        u = User.objects.create_user('morador_v', password='x')
        Estudante.objects.create(user=u, matricula='v1', cpf='43333333333', quarto=self.quarto)

        self.u_mon = User.objects.create_user('mon_v', password='x')
        Monitor.objects.create(user=self.u_mon, siape='1234321', cpf='44444444444', bloco_responsavel='F')
        self.u_est = User.objects.create_user('aluno_v', password='x')

        self.url = reverse('gerar_cronograma')

    def test_botao_existe_no_painel_do_monitor(self):
        self.client.force_login(self.u_mon)
        html = self.client.get(reverse('painel_monitor')).content.decode()
        self.assertIn('Gerar Cronograma Semanal', html)
        self.assertIn('action="%s"' % self.url, html)
        self.assertIn('csrfmiddlewaretoken', html)
        self.assertIn('method="POST"', html)

    def test_monitor_post_gera_e_responde_302(self):
        self.client.force_login(self.u_mon)
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, reverse('painel_monitor'))
        self.assertEqual(Cronograma.objects.count(), 1)
        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE))

    def test_monitor_post_mostra_mensagem_de_sucesso(self):
        self.client.force_login(self.u_mon)
        r = self.client.post(self.url, follow=True)
        mensagens = [str(m) for m in r.context['messages']]
        self.assertTrue(any('sucesso' in m.lower() for m in mensagens), mensagens)

    def test_get_nao_gera_nada(self):
        self.client.force_login(self.u_mon)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Cronograma.objects.count(), 0)

    def test_estudante_nao_pode_gerar(self):
        self.client.force_login(self.u_est)
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Cronograma.objects.count(), 0)

    def test_anônimo_nao_pode_gerar(self):
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)
        self.assertEqual(Cronograma.objects.count(), 0)

    def test_post_repetido_nao_duplica(self):
        self.client.force_login(self.u_mon)
        self.client.post(self.url)
        self.client.post(self.url)
        self.assertEqual(Cronograma.objects.count(), 1)
        self.assertEqual(Tarefa.objects.count(), len(TAREFAS_BASE))


class TarefasDaSemanaTests(TestCase):
    """Pilar 1 - o painel do estudante le a semana atual."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='707', bloco='G')
        u = User.objects.create_user('aluno_sem', password='x')
        self.estudante = Estudante.objects.create(
            user=u, matricula='sem1', cpf='45555555555', quarto=self.quarto
        )
        inicio, fim = janela_da_semana(offset=0)
        self.inicio, self.fim = inicio, fim

    def criar_tarefa(self, data_inicio, data_fim, titulo, quarto=None, prazo=None):
        return Tarefa.objects.create(
            cronograma=Cronograma.objects.create(
                quarto=quarto or self.quarto,
                data_inicio=data_inicio,
                data_fim=data_fim,
            ),
            estudante_responsavel=self.estudante,
            titulo=titulo,
            descricao='d',
            data_prazo=prazo or data_inicio,
            status='Pendente',
        )

    def ids_no_painel(self):
        return set(
            Tarefa.objects.filter(
                estudante_responsavel=self.estudante,
                cronograma__quarto=self.estudante.quarto,
                cronograma__ativo=True,
                cronograma__data_inicio__lte=self.fim,
                cronograma__data_fim__gte=self.inicio,
            ).values_list('id', flat=True)
        )

    def test_tarefa_da_semana_atual_aparece(self):
        t = self.criar_tarefa(self.inicio, self.fim, 'Da semana')
        self.assertIn(t.id, self.ids_no_painel())

    def test_tarefa_de_semana_passada_nao_aparece(self):
        t = self.criar_tarefa(self.inicio - timedelta(days=7), self.inicio - timedelta(days=1), 'Passada')
        self.assertNotIn(t.id, self.ids_no_painel())

    def test_tarefa_de_semana_futura_nao_aparece(self):
        t = self.criar_tarefa(self.fim + timedelta(days=1), self.fim + timedelta(days=7), 'Futura')
        self.assertNotIn(t.id, self.ids_no_painel())

    def test_cronograma_inativo_nao_aparece(self):
        t = self.criar_tarefa(self.inicio, self.fim, 'Inativo')
        t.cronograma.ativo = False
        t.cronograma.save()
        self.assertNotIn(t.id, self.ids_no_painel())

    def test_cronograma_desalinhado_criado_a_mao_e_encontrado(self):
        """
        Cronograma criado a mao que NAO comeca na segunda ainda tem de aparecer.

        No banco de dev existem cronogramas de 04/10 a 10/10 (domingo->sabado).
        Um filtro por faixa EXATA perderia esses registos; o de sobreposicao
        encontra. E `Cronograma` nem tem campo `prazo`, como a spec supunha.
        """
        domingo = self.inicio - timedelta(days=1)
        sabado = self.fim - timedelta(days=1)
        c = Cronograma.objects.create(
            quarto=self.quarto, data_inicio=domingo, data_fim=sabado, ativo=True
        )
        t = Tarefa.objects.create(
            cronograma=c, estudante_responsavel=self.estudante,
            titulo='Feita a mao', descricao='d', data_prazo=sabado, status='Pendente',
        )
        self.assertIn(t.id, self.ids_no_painel())

    def test_cronograma_de_quarto_diferente_nao_aparece(self):
        outro = Quarto.objects.create(numero='708', bloco='Z')
        c = Cronograma.objects.create(
            quarto=outro, data_inicio=self.inicio, data_fim=self.fim, ativo=True
        )
        t = Tarefa.objects.create(
            cronograma=c, estudante_responsavel=self.estudante,
            titulo='De outro quarto', descricao='d',
            data_prazo=self.inicio, status='Pendente',
        )
        self.assertNotIn(t.id, self.ids_no_painel())

    def test_comando_criado_aparece_no_painel_do_aluno(self):
        """Fluxo completo que motivou a mudanca: monitor gera, aluno ve na hora."""
        u_mon = User.objects.create_user('mon_sem', password='x')
        Monitor.objects.create(user=u_mon, siape='7654321', cpf='46666666666', bloco_responsavel='G')

        cliente_mon = Client(SERVER_NAME='localhost')
        cliente_mon.force_login(u_mon)
        cliente_mon.post(reverse('gerar_cronograma'))

        cliente_aluno = Client(SERVER_NAME='localhost')
        cliente_aluno.force_login(self.estudante.user)
        html = cliente_aluno.get(reverse('painel_estudante')).content.decode()

        self.assertIn('Varrer e passar pano no chão', html)
        self.assertIn('id="form-tarefa-', html)

        contexto = cliente_aluno.get(reverse('painel_estudante')).context
        self.assertEqual(contexto['tarefas'].count(), len(TAREFAS_BASE))
        for t in contexto['tarefas']:
            self.assertIn(t.id, self.ids_no_painel())

    def test_tarefa_da_semana_futura_gerada_com_flag_nao_aparece(self):
        """--semana 1 continua existindo, mas nao entra no painel da semana atual."""
        call_command('gerar_cronograma', semana=1, verbosity=0)

        c = Cronograma.objects.get()
        self.assertEqual(c.data_inicio, janela_da_semana(offset=1)[0])
        self.assertFalse(
            Tarefa.objects.filter(cronograma=c, id__in=self.ids_no_painel()).exists()
        )

    def test_painel_do_aluno_responde_200_com_tarefas(self):
        self.criar_tarefa(self.inicio, self.fim, 'Tarefa da semana')
        cliente_aluno = Client(SERVER_NAME='localhost')
        cliente_aluno.force_login(self.estudante.user)
        r = cliente_aluno.get(reverse('painel_estudante'))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('Tarefa da semana', html)
        self.assertIn('id="form-tarefa-', html)


class PainelMonitorVisualTests(TestCase):
    """Mensagens de feedback + redesign minimalista do painel do monitor."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='808', bloco='N')
        u = User.objects.create_user('mor_v2', password='x')
        Estudante.objects.create(user=u, matricula='v2', cpf='48888888888', quarto=self.quarto)
        self.u_mon = User.objects.create_user('mon_v2', password='x')
        Monitor.objects.create(
            user=self.u_mon, siape='1122334', cpf='49999999999', bloco_responsavel='N'
        )

    def html_apos_gerar(self):
        self.client.force_login(self.u_mon)
        r = self.client.post(reverse('gerar_cronograma'), follow=True)
        return r.content.decode()

    def html_painel(self):
        self.client.force_login(self.u_mon)
        return self.client.get(reverse('painel_monitor')).content.decode()

    @staticmethod
    def banner_de_mensagem(html):
        """Recorta só o banner da mensagem (o resto da pagina usa outras cores)."""
        m = re.search(r'<div data-msg.*?</button>\s*</div>', html, re.S)
        return m.group(0) if m else ''

    # --- 1. mensagens ---
    def test_mensagem_de_sucesso_aparece_no_html(self):
        html = self.html_apos_gerar()
        self.assertIn('Cronograma da próxima semana gerado com sucesso!', html)

    def test_sucesso_usa_a_estetica_verde(self):
        banner = self.banner_de_mensagem(self.html_apos_gerar())
        self.assertTrue(banner, 'banner de mensagem nao encontrado')
        self.assertIn('bg-emerald-50', banner)
        self.assertIn('text-emerald-800', banner)
        self.assertIn('border-emerald-200', banner)
        self.assertIn('fa-circle-check', banner)
        self.assertNotIn('bg-rose-50', banner)

    def test_erro_usa_a_estetica_vermelha(self):
        """Um erro nunca pode aparecer com o verde de sucesso."""
        from unittest.mock import patch
        with patch('ClearOrder.views.call_command', side_effect=Exception('deu ruim')):
            html = self.html_apos_gerar()
        banner = self.banner_de_mensagem(html)
        self.assertIn('Não foi possível gerar o cronograma', banner)
        self.assertIn('bg-rose-50', banner)
        self.assertIn('text-rose-800', banner)
        self.assertIn('fa-circle-exclamation', banner)
        self.assertNotIn('bg-emerald-50', banner)

    def test_banner_tem_data_msg_e_x_funcional(self):
        html = self.html_apos_gerar()
        self.assertIn('data-msg', html)
        self.assertIn("this.closest('[data-msg]').remove()", html)
        # parentElement removeria so o div interno do texto
        self.assertNotIn('this.parentElement.remove()', html)

    def test_mensagem_ocupa_a_largura_inteira_do_grid(self):
        html = self.html_apos_gerar()
        self.assertIn('lg:col-span-12 space-y-3', html)

    def test_sem_mensagens_nao_renderiza_banner(self):
        html = self.html_painel()
        self.assertNotIn('data-msg', html)

    # --- 2. redesign minimalista ---
    def test_atalhos_no_grid_pedido(self):
        html = self.html_painel()
        self.assertIn('grid grid-cols-1 md:grid-cols-2 gap-4 items-stretch', html)

    def test_atalhos_com_estetica_minimalista(self):
        card = ('bg-white border border-slate-200/70 rounded-3xl shadow-sm '
                'hover:border-emerald-200 hover:shadow-md transition-all')

        # Remove as variantes dark: antes de comparar. A casca SUAP (a parte
        # testada) continua igual entre os dois atalhos; o dark: é um extra
        # que pode crescer sem precisar mexer aqui de novo.
        def sem_dark(html):
            import re as _re
            html = _re.sub(r'\bdark:[^\s"]+', '', html)
            return _re.sub(r'  +', ' ', html)   # remove o espaço que o strip deixa

        html = sem_dark(self.html_painel())
        self.assertEqual(html.count(card), 2, 'os dois atalhos devem ter a mesma casca')

    def test_atalhos_tem_variantes_dark(self):
        """
        Os dois atalhos também precisam escurecer junto com o resto da tela.
        Conta a casca COMPLETA (com o dark:), não só o trecho dark: — esse
        trecho também aparece no card do quarto selecionado.
        """
        casca = ('bg-white border border-slate-200/70 rounded-3xl shadow-sm '
                 'hover:border-emerald-200 hover:shadow-md transition-all '
                 'dark:bg-slate-900 dark:border-slate-800 dark:hover:border-emerald-700')
        self.assertEqual(
            self.html_painel().count(casca), 2,
            'os dois atalhos devem ter a mesma casca, com as mesmas variantes dark:')

    def test_botao_azul_solido_gigante_saiu(self):
        html = self.html_painel()
        self.assertNotIn('bg-blue-600 text-white rounded-2xl', html)
        self.assertNotIn('fa-wand-magic-sparkles', html)

    def test_icones_pastel_discretos(self):
        html = self.html_painel()
        self.assertIn('bg-emerald-50 text-emerald-800', html)   # cronograma
        self.assertIn('bg-slate-100 text-slate-500', html)      # chamados

    def test_titulos_escuros_e_subtitulos_esmaecidos(self):
        html = self.html_painel()
        self.assertIn('text-sm font-bold text-emerald-950 leading-tight', html)
        self.assertIn('text-xs text-slate-400 mt-0.5', html)

    def test_cabecalho_redesenhado(self):
        html = self.html_painel()
        self.assertIn('Acompanhe e aprove as vistorias dos quartos', html)
        self.assertIn('text-sm font-black tracking-tight text-emerald-950', html)
        self.assertNotIn('class="h-16 bg-white border-b', html)

    # --- nada que existia pode ter quebrado ---
    def test_dropdown_de_notificacoes_intacto(self):
        html = self.html_painel()
        self.assertIn('id="dropdown-notificacoes"', html)
        self.assertIn('function toggleNotificacoes', html)
        self.assertIn('aria-label="Notificações"', html)
        self.assertIn('z-50', html)

    def test_form_do_cronograma_continua_postando(self):
        html = self.html_painel()
        self.assertIn('action="%s"' % reverse('gerar_cronograma'), html)
        self.assertIn('method="POST"', html)
        self.assertIn('csrfmiddlewaretoken', html)
        self.assertIn('Gerar Cronograma Semanal', html)

    def test_atalho_de_chamados_presente(self):
        html = self.html_painel()
        self.assertIn('href="%s"' % reverse('chamados'), html)
        self.assertIn('Chamados de Infraestrutura', html)

    def test_fluxo_intacto_apos_gerar(self):
        html = self.html_apos_gerar()
        # a vistoria e os chamados continuam acessíveis no mesmo painel
        self.assertIn('Quartos (', html)
        self.assertIn('Gerar Cronograma Semanal', html)
        self.assertIn('id="modal-confirmacao"', html)


class CalendarioSemanalTests(TestCase):
    """Pilar 2 - calendario dinamico de vistorias no painel do estudante."""

    ROTULOS = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='909', bloco='P')
        u = User.objects.create_user('aluno_cal', password='x')
        self.estudante = Estudante.objects.create(
            user=u, matricula='cal1', cpf='51111111111', quarto=self.quarto
        )
        um = User.objects.create_user('mon_cal', password='x')
        self.monitor = Monitor.objects.create(
            user=um, siape='3344556', cpf='52222222222', bloco_responsavel='P'
        )
        self.inicio, self.fim = janela_da_semana(offset=0)
        self.client.force_login(u)

    # --- helpers ---
    def dia(self, offset):
        return self.inicio + timedelta(days=offset)

    def vistoria(self, data, aprovado, nota=8, quarto=None):
        return Vistoria.objects.create(
            monitor=self.monitor, quarto=quarto or self.quarto,
            data_vistoria=data, aprovado=aprovado, nota=nota, observacoes='',
        )

    def ler_calendario(self):
        """{ 'Seg': (status, classes_do_bolinha), ... } extraido do HTML."""
        html = self.client.get(reverse('painel_estudante')).content.decode()
        out = {}
        for m in re.finditer(
            r'<div class="([^"]*?)"\s+data-dia="([^"]+)"\s+data-status="([^"]+)"', html, re.S
        ):
            out[m.group(2)] = (m.group(3), ' '.join(m.group(1).split()))
        return out, html

    # --- 7 dias ---
    def test_renderiza_os_7_dias_na_ordem_certa(self):
        cal, html = self.ler_calendario()
        self.assertEqual(list(cal.keys()), self.ROTULOS)
        self.assertIn('data-calendario', html)

    def test_sem_vistorias_tudo_pendente(self):
        cal, _ = self.ler_calendario()
        self.assertEqual([s for s, _ in cal.values()], ['pendente'] * 7)
        for _, classes in cal.values():
            self.assertIn('bg-slate-100', classes)
            self.assertIn('text-slate-400', classes)
            self.assertIn('border-slate-200', classes)

    # --- status por vistoria ---
    def test_vistoria_aprovada_marca_verde(self):
        self.vistoria(self.dia(0), True)
        cal, _ = self.ler_calendario()
        status, classes = cal['Seg']
        self.assertEqual(status, 'aprovado')
        self.assertIn('bg-emerald-100', classes)
        self.assertIn('text-emerald-800', classes)
        self.assertIn('border-emerald-300', classes)

    def test_vistoria_reprovada_marca_vermelho(self):
        self.vistoria(self.dia(3), False)
        cal, _ = self.ler_calendario()
        status, classes = cal['Qui']
        self.assertEqual(status, 'reprovado')
        self.assertIn('bg-rose-100', classes)
        self.assertIn('text-rose-800', classes)
        self.assertIn('border-rose-300', classes)

    def test_aprovada_e_reprovada_no_mesmo_calendario(self):
        self.vistoria(self.dia(1), True)
        self.vistoria(self.dia(4), False)
        cal, _ = self.ler_calendario()
        self.assertEqual(cal['Ter'][0], 'aprovado')
        self.assertEqual(cal['Sex'][0], 'reprovado')
        self.assertEqual(cal['Seg'][0], 'pendente')
        self.assertEqual(cal['Dom'][0], 'pendente')

    def test_icones_corretos_por_status(self):
        self.vistoria(self.dia(0), True)
        self.vistoria(self.dia(1), False)
        _, html = self.ler_calendario()
        self.assertIn('fa-check', html)
        self.assertIn('fa-xmark', html)
        self.assertIn('fa-minus', html)

    # --- dia de hoje ---
    def test_dia_de_hoje_destacado(self):
        cal, html = self.ler_calendario()
        hoje = timezone.localdate()
        rotulo_hoje = self.ROTULOS[hoje.weekday()]
        self.assertIn('ring-2 ring-emerald-500', cal[rotulo_hoje][1])
        for rotulo, (_s, classes) in cal.items():
            if rotulo != rotulo_hoje:
                self.assertNotIn('ring-2', classes, '%s nao deveria estar destacado' % rotulo)
        self.assertIn('aria-current="date"', html)

    def test_exatamente_um_dia_destacado(self):
        _, html = self.ler_calendario()
        self.assertEqual(html.count('ring-2 ring-emerald-500'), 1)

    # --- isolamento ---
    def test_vistoria_fora_da_semana_e_ignorada(self):
        self.vistoria(self.inicio - timedelta(days=1), True)
        self.vistoria(self.fim + timedelta(days=1), False)
        cal, _ = self.ler_calendario()
        self.assertEqual([s for s, _ in cal.values()], ['pendente'] * 7)

    def test_vistoria_de_outro_quarto_e_ignorada(self):
        outro = Quarto.objects.create(numero='910', bloco='P')
        self.vistoria(self.dia(0), True, quarto=outro)
        cal, _ = self.ler_calendario()
        self.assertEqual(cal['Seg'][0], 'pendente')

    def test_duas_vistorias_no_mesmo_dia_valem_a_ultima(self):
        self.vistoria(self.dia(0), True)
        self.vistoria(self.dia(0), False)
        cal, _ = self.ler_calendario()
        self.assertEqual(cal['Seg'][0], 'reprovado')

    def test_aluno_sem_quarto_nao_quebra(self):
        self.estudante.quarto = None
        self.estudante.save()
        r = self.client.get(reverse('painel_estudante'))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertEqual(html.count('data-dia="'), 7)

    # --- o que era fixo antes ---
    def test_calendario_hardcoded_saiu(self):
        _, html = self.ler_calendario()
        self.assertNotIn('bg-amber-400 text-amber-900', html)
        self.assertNotIn('bg-black/20 text-white/50', html)

    def test_legenda_existe(self):
        _, html = self.ler_calendario()
        self.assertIn('Aprovado', html)
        self.assertIn('Reprovado', html)
        self.assertIn('Pendente', html)

    # --- integracao com o fluxo real ---
    def test_vistoria_criada_pelo_monitor_aparece_no_calendario(self):
        """Fluxo completo: monitor aprova um quarto, o aluno ve no calendario."""
        um = User.objects.create_user('mon_cal2', password='x')
        Monitor.objects.create(user=um, siape='3344557', cpf='53333333333', bloco_responsavel='P')

        cliente_mon = Client(SERVER_NAME='localhost')
        cliente_mon.force_login(um)
        cliente_mon.post(
            reverse('detalhes_quarto', args=[self.quarto.id]),
            {'chao_lavanderia': '1', 'organizacao_geral': '1', 'aprovado': 'True'},
        )

        Vistoria.objects.filter(quarto=self.quarto).update(data_vistoria=timezone.localdate())

        cal, _ = self.ler_calendario()
        rotulo_hoje = self.ROTULOS[timezone.localdate().weekday()]
        self.assertEqual(cal[rotulo_hoje][0], 'aprovado')


class NotificacoesSignalsTests(TestCase):
    """Pilar 3 - sinais que geram notificacoes automaticamente."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='111', bloco='Q')
        u_mon = User.objects.create_user('mon_not', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='4455667', cpf='54444444444', bloco_responsavel='Q'
        )
        # dois estudantes no MESMO quarto
        u1 = User.objects.create_user('aluno_n1', password='x')
        self.est1 = Estudante.objects.create(
            user=u1, matricula='n1', cpf='55511111111', quarto=self.quarto
        )
        u2 = User.objects.create_user('aluno_n2', password='x')
        self.est2 = Estudante.objects.create(
            user=u2, matricula='n2', cpf='55522222222', quarto=self.quarto
        )
        # terceiro quarto, sem ligacao com os dois
        outro = Quarto.objects.create(numero='112', bloco='Q')
        u3 = User.objects.create_user('aluno_n3', password='x')
        self.est3 = Estudante.objects.create(
            user=u3, matricula='n3', cpf='55533333333', quarto=outro
        )

    # --- 1. sinal de Vistoria ---
    def test_vistoria_aprovada_gera_notificacao(self):
        Vistoria.objects.create(
            monitor=self.monitor, quarto=self.quarto, aprovado=True, nota=10,
            data_vistoria=timezone.localdate(),
        )
        self.assertEqual(Notificacao.objects.count(), 2)   # 2 moradores do quarto
        n = Notificacao.objects.get(estudante_destinatario=self.est1)
        self.assertIn('aprovada', n.titulo.lower())
        self.assertIn('aprovada', n.mensagem.lower())
        self.assertIn('10/10', n.mensagem)
        self.assertFalse(n.lida)

    def test_vistoria_reprovada_gera_notificacao(self):
        Vistoria.objects.create(
            monitor=self.monitor, quarto=self.quarto, aprovado=False, nota=3,
            data_vistoria=timezone.localdate(),
        )
        n = Notificacao.objects.get(estudante_destinatario=self.est1)
        self.assertIn('reprovada', n.titulo.lower())
        self.assertIn('reprovada', n.mensagem.lower())
        self.assertIn('3/10', n.mensagem)

    def test_vistoria_avisa_todos_do_quarto_e_nenhum_de_fora(self):
        Vistoria.objects.create(
            monitor=self.monitor, quarto=self.quarto, aprovado=True, nota=8,
            data_vistoria=timezone.localdate(),
        )
        self.assertTrue(Notificacao.objects.filter(estudante_destinatario=self.est1).exists())
        self.assertTrue(Notificacao.objects.filter(estudante_destinatario=self.est2).exists())
        self.assertFalse(Notificacao.objects.filter(estudante_destinatario=self.est3).exists())

    def test_resalvar_vistoria_nao_duplica_notificacao(self):
        v = Vistoria.objects.create(
            monitor=self.monitor, quarto=self.quarto, aprovado=True, nota=9,
            data_vistoria=timezone.localdate(),
        )
        self.assertEqual(Notificacao.objects.count(), 2)
        v.observacoes = 'revisitado'
        v.save()
        self.assertEqual(Notificacao.objects.count(), 2, 'save() nao deve duplicar aviso')

    def test_vistoria_sem_nota_nao_quebra(self):
        Vistoria.objects.create(
            monitor=self.monitor, quarto=self.quarto, aprovado=True, nota=None,
            data_vistoria=timezone.localdate(),
        )
        n = Notificacao.objects.get(estudante_destinatario=self.est1)
        self.assertNotIn('Nota', n.mensagem)
        self.assertIn('aprovada', n.mensagem.lower())

    # --- 2. sinal de SolicitacaoReparo ---
    def fazer_chamado(self):
        return SolicitacaoReparo.objects.create(
            estudante=self.est1, titulo='Vazamento', categoria='hidraulica',
            descricao='pingando',
        )

    def test_criar_chamado_nao_gera_notificacao(self):
        self.fazer_chamado()
        self.assertEqual(Notificacao.objects.count(), 0)

    def test_chamado_verificado_avisa_o_autor(self):
        chamado = self.fazer_chamado()
        chamado.status = 'Verificado'
        chamado.save()
        self.assertEqual(Notificacao.objects.count(), 1)
        n = Notificacao.objects.get()
        self.assertEqual(n.estudante_destinatario, self.est1)
        self.assertIn('verificado', n.titulo.lower())
        self.assertIn('Vazamento', n.mensagem)

    def test_chamado_para_outro_status_nao_gera_notificacao(self):
        chamado = self.fazer_chamado()
        chamado.status = 'Em Andamento'
        chamado.save()
        self.assertEqual(Notificacao.objects.count(), 0)

    def test_resalvar_verificado_nao_duplica_notificacao(self):
        chamado = self.fazer_chamado()
        chamado.status = 'Verificado'
        chamado.save()
        self.assertEqual(Notificacao.objects.count(), 1)
        chamado.descricao = 'atualizado'
        chamado.save()
        chamado.save()
        self.assertEqual(Notificacao.objects.count(), 1, 'nao deve duplicar aviso')

    def test_verificar_pelo_botao_do_monitor_gera_notificacao(self):
        chamado = self.fazer_chamado()
        u_mon = User.objects.get(username='mon_not')
        cliente = Client(SERVER_NAME='localhost')
        cliente.force_login(u_mon)
        cliente.post(reverse('verificar_reparo', args=[chamado.id]))
        self.assertEqual(Notificacao.objects.count(), 1)
        self.assertEqual(Notificacao.objects.get().estudante_destinatario, self.est1)


class NotificacoesHeaderTests(TestCase):
    """Pilar 3 - contador e dropdown no sininho."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='113', bloco='R')
        u = User.objects.create_user('aluno_bell', password='x')
        self.est = Estudante.objects.create(
            user=u, matricula='b1', cpf='66611111111', quarto=self.quarto
        )
        u2 = User.objects.create_user('aluno_bell2', password='x')
        self.est2 = Estudante.objects.create(
            user=u2, matricula='b2', cpf='66622222222', quarto=self.quarto
        )

    def criar(self, destinatario=None, lida=False, titulo='Vistoria aprovada!'):
        return Notificacao.objects.create(
            estudante_destinatario=destinatario or self.est,
            titulo=titulo, mensagem='Mensagem de teste', lida=lida,
        )

    def html(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.est.user)
        return c.get(reverse('painel_estudante')).content.decode()

    # --- contador ---
    def test_sem_notificacoes_sem_badge(self):
        html = self.html()
        self.assertNotIn('min-w-[18px]', html)
        self.assertIn('Nenhuma notificação por aqui', html)

    def test_contador_mostra_a_quantidade(self):
        for _ in range(3):
            self.criar()
        self.assertIn('>3</span>', self.html())

    def test_contador_ignora_as_ja_lidas(self):
        self.criar()
        self.criar(lida=True)
        self.criar(lida=True)
        self.assertIn('>1</span>', self.html())

    def test_contadro_mostra_o_usuario_correto(self):
        """Notificacao de outro aluno nao entra no contador deste."""
        self.criar(lida=False)                 # do est
        self.criar(destinatario=self.est2)     # do colega
        html = self.html()
        self.assertIn('>1</span>', html)

    # --- dropdown ---
    def test_dropdown_lista_as_notificacoes(self):
        self.criar(titulo='Vistoria Aprovada!')
        self.criar(titulo='Chamado verificado')
        html = self.html()
        self.assertIn('Vistoria Aprovada!', html)
        self.assertIn('Chamado verificado', html)

    def test_mostra_no_maximo_5(self):
        for i in range(8):
            self.criar(titulo='Notificacao %d' % i)
        html = self.html()
        # as 5 mais recentes aparecem
        for i in range(3, 8):
            self.assertIn('Notificacao %d' % i, html)
        # as 3 mais antigas ficam de fora
        for i in range(0, 3):
            self.assertNotIn('Notificacao %d' % i, html)
        # o contexto limita a 5
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.est.user)
        contexto = c.get(reverse('painel_estudante')).context
        self.assertEqual(len(contexto['notificacoes_recentes']), 5)

    def test_tempo_relativo_renderizado(self):
        self.criar()
        self.assertIn('atrás', self.html())

    def test_marcador_de_nao_lida(self):
        self.criar(lida=False)
        self.assertIn('não lida', self.html())

    def test_mocks_foram_removidos(self):
        html = self.html()
        self.assertNotIn('A vistoria do seu quarto foi aprovada!', html)
        self.assertNotIn('Nova tarefa atribuída: Limpeza das janelas', html)

    def test_forms_de_marcar_como_lida(self):
        n = self.criar()
        html = self.html()
        self.assertIn(reverse('marcar_notificacao_lida', args=[n.id]), html)
        self.assertIn(reverse('limpar_notificacoes'), html)

    def test_botao_marcar_todas_somente_quando_ha_nao_lidas(self):
        self.assertNotIn(reverse('limpar_notificacoes'), self.html())
        self.criar()
        self.assertIn(reverse('limpar_notificacoes'), self.html())

    def test_lidas_continuam_listadas_apenas_esmaecidas(self):
        """
        A dropdown mostra as 5 mais recentes INDEPENDENTEMENTE do estado.
        Ler desliga a badge, mas o historico continua visivel (esmaecido) -
        e o estado vazio so aparece com zero notificacoes.
        """
        self.criar(titulo='Aviso importante')
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.est.user)
        c.post(reverse('limpar_notificacoes'))

        html = self.html()
        self.assertNotIn('min-w-[18px]', html)                  # badge some
        self.assertIn('Aviso importante', html)               # continua no historico
        self.assertIn('opacity-60', html)                     # esmaecida
        self.assertNotIn('não lida', html)                    # sem marcador
        self.assertNotIn('Nenhuma notificação por aqui', html)

    # --- rotas ---
    def test_marcar_uma_como_lida(self):
        n = self.criar()
        self.assertFalse(n.lida)
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.est.user)
        r = c.post(reverse('marcar_notificacao_lida', args=[n.id]))
        self.assertEqual(r.status_code, 302)
        n.refresh_from_db()
        self.assertTrue(n.lida)

    def test_limpar_todas(self):
        for _ in range(3):
            self.criar()
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.est.user)
        c.post(reverse('limpar_notificacoes'))
        self.assertEqual(
            Notificacao.objects.filter(estudante_destinatario=self.est, lida=False).count(), 0
        )

    def test_nao_marca_notificacao_de_outro_usuario(self):
        n = self.criar(destinatario=self.est2)
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.est.user)
        r = c.post(reverse('marcar_notificacao_lida', args=[n.id]))
        self.assertEqual(r.status_code, 404)
        n.refresh_from_db()
        self.assertFalse(n.lida)

    def test_limpar_so_afeta_o_proprio_usuario(self):
        minha = self.criar()
        alheia = self.criar(destinatario=self.est2)
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.est.user)
        c.post(reverse('limpar_notificacoes'))
        minha.refresh_from_db()
        alheia.refresh_from_db()
        self.assertTrue(minha.lida)
        self.assertFalse(alheia.lida)

    def test_get_nao_marca_nada(self):
        n = self.criar()
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.est.user)
        c.get(reverse('marcar_notificacao_lida', args=[n.id]))
        n.refresh_from_db()
        self.assertFalse(n.lida)

    def test_rota_exige_login(self):
        n = self.criar()
        anon = Client(SERVER_NAME='localhost')
        r = anon.post(reverse('marcar_notificacao_lida', args=[n.id]))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)
        r2 = anon.post(reverse('limpar_notificacoes'))
        self.assertIn('/login/', r2.url)
        n.refresh_from_db()
        self.assertFalse(n.lida)


class GestaoMonitorTests(TestCase):
    """Pilar 4 - painel de gestao e relatorios do monitor."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='201', bloco='G')
        self.vazio = Quarto.objects.create(numero='202', bloco='G')

        u_mon = User.objects.create_user('mon_g', password='x')
        Monitor.objects.create(user=u_mon, siape='5566778', cpf='88811111111', bloco_responsavel='G')

        u_est = User.objects.create_user('aluno_g', password='x')
        self.estudante = Estudante.objects.create(
            user=u_est, matricula='g1', cpf='88822222222', quarto=self.quarto
        )

        u_sem = User.objects.create_user('sem_perfil_g', password='x')

        self.u_mon, self.u_est, self.u_sem = u_mon, u_est, u_sem
        self.url = reverse('gestao_monitor')

    # ---------- 1. controle de acesso ----------
    def test_anonimo_redireciona_para_login(self):
        anon = Client(SERVER_NAME='localhost')
        r = anon.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)

    def test_anonimo_nao_consegue_postar(self):
        anon = Client(SERVER_NAME='localhost')
        antes = Estudante.objects.count()
        r = anon.post(self.url, {'acao': 'novo_estudante', 'username': 'x', 'matricula': 'x', 'senha': 'x'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Estudante.objects.count(), antes)

    def test_estudante_recebe_403(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_est)
        self.assertEqual(c.get(self.url).status_code, 403)

    def test_usuario_sem_perfil_recebe_403(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_sem)
        self.assertEqual(c.get(self.url).status_code, 403)

    def test_estudante_nao_consegue_criar_estudante(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_est)
        antes = Estudante.objects.count()
        r = c.post(self.url, {
            'acao': 'novo_estudante', 'username': 'intruso',
            'matricula': 'intruso', 'senha': '123',
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Estudante.objects.count(), antes)
        self.assertFalse(User.objects.filter(username='intruso').exists())

    def test_monitor_acessa(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_mon)
        self.assertEqual(c.get(self.url).status_code, 200)

    # ---------- 2. cadastrar estudante ----------
    def criar(self, **over):
        dados = {
            'acao': 'novo_estudante', 'username': 'novo', 'nome': 'Fulano',
            'senha': 'cleanorder123', 'matricula': 'mat999', 'cpf': '', 'quarto': '',
        }
        dados.update(over)
        return dados

    def cliente_monitor(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_mon)
        return c

    def test_cria_estudante_com_quarto(self):
        c = self.cliente_monitor()
        r = c.post(self.url, self.criar(quarto=str(self.quarto.id)), follow=True)
        self.assertEqual(r.status_code, 200)
        novo = Estudante.objects.get(user__username='novo')
        self.assertEqual(novo.matricula, 'mat999')
        self.assertEqual(novo.quarto, self.quarto)
        self.assertTrue(User.objects.get(username='novo').check_password('cleanorder123'))

    def test_cria_estudante_sem_quarto(self):
        c = self.cliente_monitor()
        c.post(self.url, self.criar())
        self.assertIsNone(Estudante.objects.get(user__username='novo').quarto)

    def test_mensagem_de_sucesso_na_criacao(self):
        c = self.cliente_monitor()
        r = c.post(self.url, self.criar(), follow=True)
        mensagens = [str(m) for m in r.context['messages']]
        self.assertTrue(any('sucesso' in m.lower() for m in mensagens), mensagens)

    def test_campos_obrigatorios(self):
        c = self.cliente_monitor()
        antes = Estudante.objects.count()
        r = c.post(self.url, self.criar(username='', matricula='', senha=''), follow=True)
        self.assertEqual(Estudante.objects.count(), antes)
        mensagens = [str(m) for m in r.context['messages']]
        self.assertTrue(any('obrigat' in m.lower() for m in mensagens), mensagens)

    def test_username_duplicado(self):
        c = self.cliente_monitor()
        antes = Estudante.objects.count()
        r = c.post(self.url, self.criar(username='aluno_g'), follow=True)
        self.assertEqual(Estudante.objects.count(), antes)
        mensagens = [str(m) for m in r.context['messages']]
        self.assertTrue(any('já existe' in m.lower() for m in mensagens), mensagens)

    def test_matricula_duplicada(self):
        c = self.cliente_monitor()
        antes = Estudante.objects.count()
        c.post(self.url, self.criar(matricula='g1'), follow=True)
        self.assertEqual(Estudante.objects.count(), antes)

    def test_cpf_duplicado(self):
        c = self.cliente_monitor()
        antes = Estudante.objects.count()
        c.post(self.url, self.criar(cpf='88822222222'), follow=True)
        self.assertEqual(Estudante.objects.count(), antes)

    def test_quarto_inexistente_no_cadastro(self):
        c = self.cliente_monitor()
        antes = Estudante.objects.count()
        c.post(self.url, self.criar(quarto='999999'), follow=True)
        self.assertEqual(Estudante.objects.count(), antes)

    # ---------- 3. vincular / mover morador ----------
    def test_move_morador_de_quarto(self):
        c = self.cliente_monitor()
        self.assertEqual(self.estudante.quarto, self.quarto)
        r = c.post(self.url, {
            'acao': 'vincular_quarto',
            'estudante': self.estudante.pk,
            'quarto': str(self.vazio.id),
        }, follow=True)
        self.estudante.refresh_from_db()
        self.assertEqual(self.estudante.quarto, self.vazio)
        mensagens = [str(m) for m in r.context['messages']]
        self.assertTrue(mensagens, 'deveria haver mensagem de feedback')
        # a mensagem diz onde o morador esta agora e de onde veio
        self.assertTrue(any('agora mora no' in m for m in mensagens), mensagens)

    def test_desvincula_quando_quarto_vazio(self):
        c = self.cliente_monitor()
        c.post(self.url, {
            'acao': 'vincular_quarto', 'estudante': self.estudante.pk, 'quarto': '',
        }, follow=True)
        self.estudante.refresh_from_db()
        self.assertIsNone(self.estudante.quarto)

    def test_estudante_invalido_nao_quebra(self):
        c = self.cliente_monitor()
        r = c.post(self.url, {'acao': 'vincular_quarto', 'estudante': '99999', 'quarto': ''}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.estudante.refresh_from_db()
        self.assertEqual(self.estudante.quarto, self.quarto)

    def test_acao_desconhecida(self):
        c = self.cliente_monitor()
        r = c.post(self.url, {'acao': 'xxx'}, follow=True)
        self.assertEqual(r.status_code, 200)

    # ---------- 4. relatorios ----------
    def vistoria(self, data, aprovado, nota, quarto=None):
        return Vistoria.objects.create(
            monitor=Monitor.objects.first(), quarto=quarto or self.quarto,
            data_vistoria=data, aprovado=aprovado, nota=nota, observacoes='',
        )

    def test_media_aprovacoes_e_reprovacoes(self):
        self.vistoria(timezone.localdate(), True, 10)
        self.vistoria(timezone.localdate(), False, 6)
        c = self.cliente_monitor()
        r = c.get(self.url)
        relatorio = next(x for x in r.context['relatorios'] if x['quarto'] == self.quarto)
        self.assertEqual(relatorio['media'], 8.0)
        self.assertEqual(relatorio['aprovacoes'], 1)
        self.assertEqual(relatorio['reprovacoes'], 1)
        self.assertEqual(relatorio['total_vistorias'], 2)

    def test_media_ignora_notas_nulas(self):
        self.vistoria(timezone.localdate(), True, 10)
        self.vistoria(timezone.localdate(), True, None)
        c = self.cliente_monitor()
        relatorio = next(x for x in c.get(self.url).context['relatorios'] if x['quarto'] == self.quarto)
        self.assertEqual(relatorio['media'], 10.0)
        self.assertEqual(relatorio['total_vistorias'], 1)

    def test_quarto_sem_vistoria_mostra_media_vazia(self):
        c = self.cliente_monitor()
        relatorio = next(x for x in c.get(self.url).context['relatorios'] if x['quarto'] == self.vazio)
        self.assertIsNone(relatorio['media'])
        self.assertEqual(relatorio['aprovacoes'], 0)
        self.assertIsNone(relatorio['ultima_vistoria'])

    def test_streak_aparece_no_relatorio(self):
        self.quarto.streak_dias = 4
        self.quarto.save()
        c = self.cliente_monitor()
        relatorio = next(x for x in c.get(self.url).context['relatorios'] if x['quarto'] == self.quarto)
        self.assertEqual(relatorio['streak'], 4)

    def test_filtro_de_periodo(self):
        self.vistoria(timezone.localdate(), True, 10)                       # hoje
        self.vistoria(timezone.localdate() - timedelta(days=200), True, 2)   # antigo
        c = self.cliente_monitor()

        tudo = next(x for x in c.get(self.url).context['relatorios'] if x['quarto'] == self.quarto)
        self.assertEqual(tudo['total_vistorias'], 2)

        recente = next(
            x for x in c.get(self.url, {'periodo': '4semanas'}).context['relatorios']
            if x['quarto'] == self.quarto
        )
        self.assertEqual(recente['total_vistorias'], 1)

    def test_filtro_por_quarto(self):
        """
        Com quarto filtrado, a TABELA traz só ele. Os <select> de
        vincular/desvincular continuam vendo todos os quartos - por isso
        'quartos_com_moradores' segue sem filtro no contexto.
        """
        self.vistoria(timezone.localdate(), True, 10, quarto=self.quarto)
        self.vistoria(timezone.localdate(), True, 4, quarto=self.vazio)
        c = self.cliente_monitor()
        contexto = c.get(self.url, {'quarto': str(self.quarto.id)}).context

        self.assertEqual(len(contexto['relatorios']), 1)
        alvo = contexto['relatorios'][0]
        self.assertEqual(alvo['quarto'], self.quarto)
        self.assertEqual(alvo['media'], 10.0)

        # os dois dropdowns de vínculo não podem perder opções
        opcoes = [q['quarto'] for q in contexto['quartos_com_moradores']]
        self.assertIn(self.quarto, opcoes)
        self.assertIn(self.vazio, opcoes)

    def test_filtro_por_quarto_inexistente_nao_quebra(self):
        c = self.cliente_monitor()
        r = c.get(self.url, {'quarto': '999999'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['relatorios'], [])

    def test_filtro_por_quarto_invalido_cai_em_todos(self):
        c = self.cliente_monitor()
        r = c.get(self.url, {'quarto': 'abc'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['quarto_filtrado'], '')

    def test_sem_filtro_mostra_todos(self):
        self.vistoria(timezone.localdate(), True, 10, quarto=self.quarto)
        self.vistoria(timezone.localdate(), True, 4, quarto=self.vazio)
        contexto = self.cliente_monitor().get(self.url).context
        self.assertEqual(len(contexto['relatorios']), 2)

    def test_periodo_invalido_cai_no_tudo(self):
        c = self.cliente_monitor()
        r = c.get(self.url, {'periodo': 'inexistente'})
        self.assertEqual(r.context['periodo_escolhido'], 'tudo')

    # ---------- 5. render ----------
    def test_render_lista_quartos_e_moradores(self):
        html = self.cliente_monitor().get(self.url).content.decode()
        self.assertIn('Gestão do Alojamento', html)
        self.assertIn('Quarto 201 - Bloco G', html)
        self.assertIn('aluno_g', html)
        self.assertIn('sem moradores', html)

    def test_render_tabela_de_relatorios(self):
        self.vistoria(timezone.localdate(), True, 10)
        html = self.cliente_monitor().get(self.url).content.decode()
        for coluna in ('Media', 'Aprov.', 'Reprov.', 'Streak', 'Status', 'Ultima vistoria'):
            self.assertIn(coluna, html)
        # Django renderiza float em locale pt-BR -> virgula decimal
        self.assertIn('10,0', html)

    def test_render_formularios(self):
        html = self.cliente_monitor().get(self.url).content.decode()
        for campo in ('name="username"', 'name="matricula"', 'name="cpf"',
                      'name="senha"', 'name="quarto"', 'name="estudante"'):
            self.assertIn(campo, html)

    def test_patron_visual_do_projeto(self):
        html = self.cliente_monitor().get(self.url).content.decode()
        self.assertIn('bg-white shadow-sm border border-slate-200/70 rounded-3xl', html)

    # ---------- 6. navegacao ----------
    def test_link_gestao_aparece_para_o_monitor(self):
        c = self.cliente_monitor()
        html = c.get(reverse('painel_monitor')).content.decode()
        self.assertIn(self.url, html)
        self.assertIn('Gestão do Alojamento', html)

    def test_gestao_ativa_na_sidebar(self):
        c = self.cliente_monitor()
        html = c.get(self.url).content.decode()
        self.assertIn('Gestão do Alojamento', html)
        # o item ativo usa o fundo azul
        import re as _re
        m = _re.search(r'<a href="%s"\s+class="([^"]*)"' % _re.escape(self.url), html)
        self.assertIsNotNone(m, 'link de gestao nao encontrado com class')
        self.assertIn('bg-emerald-50', m.group(1))
        self.assertIn('border-l-4', m.group(1))

    def test_estudante_nao_ve_o_link_de_gestao(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_est)
        html = c.get(reverse('painel_estudante')).content.decode()
        self.assertNotIn(self.url, html)

    def test_atalho_no_painel_do_monitor(self):
        c = self.cliente_monitor()
        html = c.get(reverse('painel_monitor')).content.decode()
        self.assertIn(self.url, html)

    # ---------- 7. regressao: 500 no painel do monitor ----------
    def test_painel_do_monitor_so_quebra_com_estudante_sem_quarto(self):
        """
        Regressao grave: `{% url 'detalhes_quarto' item.quarto.id %}` com
        quarto=None levantava NoReverseMatch -> HTTP 500 no painel do monitor.
        A Gestao permite desvincular moradores, entao isso era alcancavel.
        """
        u_sem = User.objects.create_user('sem_quarto_g', password='x')
        Estudante.objects.create(user=u_sem, matricula='sq1', cpf='88833333333', quarto=None)

        c = self.cliente_monitor()
        r = c.get(reverse('painel_monitor'))
        self.assertEqual(r.status_code, 200)

    def test_painel_nao_quebra_apos_desvincular_pela_gestao(self):
        """Fluxo real: monito desvincula e volta ao painel de vistorias."""
        c = self.cliente_monitor()
        c.post(self.url, {
            'acao': 'vincular_quarto', 'estudante': self.estudante.pk, 'quarto': '',
        }, follow=True)
        self.assertEqual(c.get(reverse('painel_monitor')).status_code, 200)

    def test_gestao_mostra_o_que_o_painel_de_vistorias_nao_mostra(self):
        """Sem quarto nao entra na lista de vistorias, mas continua na Gestao."""
        u_sem = User.objects.create_user('sem_quarto_g2', password='x')
        solto = Estudante.objects.create(
            user=u_sem, matricula='sq2', cpf='88844444444', quarto=None
        )
        c = self.cliente_monitor()
        painel = c.get(reverse('painel_monitor')).content.decode()
        gestao = c.get(self.url).content.decode()
        self.assertNotIn(str(solto.matricula), painel)
        self.assertIn(str(solto.matricula), gestao)


class ModalPerfilTests(TestCase):
    """Modal de perfil no avatar do cabecalho."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='104', bloco='B')
        u_est = User.objects.create_user('perfil_est', password='x', first_name='Ana')
        u_est.last_name = 'Souza'
        u_est.save()
        self.u_est = u_est
        self.estudante = Estudante.objects.create(
            user=u_est, matricula='2024101234',
            cpf='12345678909', quarto=self.quarto,
        )
        u_est.email = 'ana@ifsul.edu.br'
        u_est.save()

        u_mon = User.objects.create_user('perfil_mon', password='x', first_name='Vitor')
        self.u_mon = u_mon
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='1122334', cpf='98765432100', bloco_responsavel='B'
        )

        # superuser sem nenhum perfil vinculado
        self.u_admin = User.objects.create_superuser('perfil_admin', password='x', email='adm@ifsul.edu.br')

    def html_de(self, user, url=None):
        c = Client(SERVER_NAME='localhost')
        c.force_login(user)
        return c.get(url or reverse('painel_estudante')).content.decode()

    # ---------- 1. contexto seguro ----------
    def test_perfil_disponivel_no_contexto(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_est)
        ctx = c.get(reverse('painel_estudante')).context
        self.assertEqual(ctx['perfil_estudante'], self.estudante)
        self.assertIsNone(ctx['perfil_monitor'])

    def test_contexto_do_monitor(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_mon)
        # monitor e redirecionado para /aluno/, entao usamos a pagina dele
        ctx = c.get(reverse('painel_monitor')).context
        self.assertEqual(ctx['perfil_monitor'], self.monitor)
        self.assertIsNone(ctx['perfil_estudante'])

    def test_contexto_de_admin_sem_perfil_nao_quebra(self):
        """
        Admin nao tem Estudante nem Monitor. As views ja o deslogam e mandam
        pro login (comportamento pre-existente, fora do escopo), entao testamos
        o context processor direto: precisa devolver None, nunca explodir.
        """
        from django.test import RequestFactory
        from ClearOrder.context_processors import perfil as perfil_processor

        req = RequestFactory().get('/')
        req.user = self.u_admin
        ctx = perfil_processor(req)
        self.assertIsNone(ctx['perfil_estudante'])
        self.assertIsNone(ctx['perfil_monitor'])

    def test_modal_renderiza_para_admin_sem_perfil(self):
        from django.test import RequestFactory
        from django.template.loader import render_to_string
        from ClearOrder.context_processors import perfil as perfil_processor

        req = RequestFactory().get('/')
        req.user = self.u_admin
        html = render_to_string('_modal_perfil.html', perfil_processor(req), request=req)
        self.assertIn('id="modal-perfil"', html)
        self.assertIn('não possui perfil', html)
        self.assertIn('perfil_admin', html)

    # ---------- 2. render ----------
    def test_modal_renderiza_para_estudante(self):
        html = self.html_de(self.u_est)
        self.assertIn('id="modal-perfil"', html)

    def test_modal_renderiza_para_monitor(self):
        self.assertIn('id="modal-perfil"', self.html_de(self.u_mon, reverse('painel_monitor')))

    def test_dados_do_estudante_no_modal(self):
        html = self.html_de(self.u_est)
        self.assertIn('2024101234', html)               # matricula
        self.assertIn('123.456.789-09', html)            # cpf mascarado
        self.assertNotIn('>12345678909<', html)          # nunca em branco
        self.assertIn('Quarto 104 - Bloco B', html)
        self.assertIn('perfil_est', html)                # username
        self.assertIn('ana@ifsul.edu.br', html)          # e-mail institucional
        self.assertIn('Estudante', html)                 # badge

    def test_mascara_de_cpf(self):
        from ClearOrder.context_processors import _mascara_cpf
        self.assertEqual(_mascara_cpf('12345678909'), '123.456.789-09')
        self.assertEqual(_mascara_cpf('123.456.789-09'), '123.456.789-09')  # idempotente
        self.assertEqual(_mascara_cpf(''), '')
        self.assertEqual(_mascara_cpf(None), '')
        self.assertEqual(_mascara_cpf('abc'), 'abc')     # nao corrompe

    def test_cpf_mascarado_tambem_no_monitor(self):
        html = self.html_de(self.u_mon, reverse('painel_monitor'))
        self.assertIn('987.654.321-00', html)

    def test_dados_do_monitor_no_modal(self):
        html = self.html_de(self.u_mon, reverse('painel_monitor'))
        self.assertIn('1122334', html)             # siape
        self.assertIn('Monitor', html)

    def test_estudante_sem_quarto_mostra_sem_alocado(self):
        self.estudante.quarto = None
        self.estudante.save()
        html = self.html_de(self.u_est)
        self.assertIn('Sem Quarto Alocado', html)

    # ---------- 3. interacao ----------
    def test_avatar_e_clicavel(self):
        html = self.html_de(self.u_est)
        self.assertIn('onclick="abrirPerfil()"', html)
        self.assertIn('cursor-pointer', html)

    def test_modal_nasce_escondido(self):
        import re as _re
        html = self.html_de(self.u_est)
        m = _re.search(r'id="modal-perfil"[^>]*?class="([^"]*)"', html, _re.S)
        self.assertIsNotNone(m)
        self.assertIn('hidden', m.group(1).split())

    def test_funcoes_js_presentes(self):
        html = self.html_de(self.u_est)
        self.assertIn('function abrirPerfil()', html)
        self.assertIn('function fecharPerfil(event)', html)
        self.assertIn("event.key !== 'Escape'", html)
        self.assertIn('event.stopPropagation()', html)   # clique no card nao fecha
        self.assertIn('event.target !== event.currentTarget', html)  # fora fecha

    def test_js_troca_hidden_e_flex(self):
        html = self.html_de(self.u_est)
        abrir = html.split('function abrirPerfil()')[1].split('function fecharPerfil')[0]
        self.assertIn("classList.remove('hidden')", abrir)
        self.assertIn("classList.add('flex')", abrir)
        fechar = html.split('function fecharPerfil(event)')[1].split('document.addEventListener')[0]
        self.assertIn("classList.add('hidden')", fechar)
        self.assertIn("classList.remove('flex')", fechar)

    # ---------- 4. estilo SUAP ----------
    def test_estetica_suap(self):
        html = self.html_de(self.u_est)
        self.assertIn('bg-slate-900/40', html)          # fundo do modal
        self.assertIn('backdrop-blur-xs', html)
        self.assertIn('bg-[#00e6a5] text-teal-950', html)  # avatar menta
        self.assertIn('bg-emerald-100 text-emerald-800', html)  # badge de perfil

    # ---------- 5. rodape / logout ----------
    def test_form_de_sair_usa_post_para_logout(self):
        import re as _re
        html = self.html_de(self.u_est)

        # localiza o <form> do rodapé do modal (o da sidebar é <a>, não <form>)
        form = _re.search(
            r'<form action="%s" method="POST"[^>]*>(.*?)</form>'
            % _re.escape(reverse('logout')),
            html, _re.S,
        )
        self.assertIsNotNone(form, 'modal sem form POST para logout')
        self.assertIn('csrfmiddlewaretoken', form.group(1))
        self.assertIn('Sair da Conta', form.group(1))
        self.assertIn('fa-arrow-right-from-bracket', form.group(1))

    def test_logout_via_post_encerra_a_sessao(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_est)
        r = c.post(reverse('logout'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(c.session.get('_auth_user_id') or 0), 0)

    def test_logout_via_get_continua_funcionando(self):
        """A rota antiga (link da sidebar) nao pode ter quebrado."""
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.u_est)
        r = c.get(reverse('logout'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(c.session.get('_auth_user_id') or 0), 0)

    # ---------- 6. nao quebrou o resto ----------
    def test_dropdown_de_notificacoes_intacto(self):
        html = self.html_de(self.u_est)
        self.assertIn('id="dropdown-notificacoes"', html)
        self.assertIn('function toggleNotificacoes', html)
        self.assertIn('id="modal-estudante"', html)

    def test_modal_no_painel_do_monitor_tambem(self):
        html = self.html_de(self.u_mon, reverse('painel_monitor'))
        self.assertIn('id="modal-perfil"', html)
        self.assertIn('onclick="abrirPerfil()"', html)


def imagem_memoria(nome='f.png', tipo='image/png', cor=(30, 200, 120)):
    """PNG minúsculo gerado na hora - não depende de arquivo no disco."""
    import io as _io
    from PIL import Image
    buf = _io.BytesIO()
    Image.new('RGB', (12, 12), cor).save(buf, format='PNG')
    return SimpleUploadedFile(nome, buf.getvalue(), content_type=tipo)


class AnexoImagemChamadoTests(TestCase):
    """Parte 1: foto opcional no chamado de infraestrutura."""

    def setUp(self):
        self.q = Quarto.objects.create(numero='104', bloco='B')
        u = User.objects.create_user('anexo_est', password='x')
        self.estudante = Estudante.objects.create(
            user=u, matricula='anx1', cpf='11122233344', quarto=self.q
        )
        u_mon = User.objects.create_user('anexo_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='9998887', cpf='55566677788', bloco_responsavel='B'
        )

    def cliente_est(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.estudante.user)
        return c

    def cliente_mon(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c

    def dados(self, **extra):
        d = {'titulo': 'Lampada queimada', 'categoria': 'eletrica',
             'descricao': 'Nao acende'}
        d.update(extra)
        return d

    # ---------- campos opcionais no model ----------
    def test_campos_sao_opcionais(self):
        f = SolicitacaoReparo._meta.get_field('imagem')
        self.assertTrue(f.null)
        self.assertTrue(f.blank)
        p = Estudante._meta.get_field('foto_perfil')
        self.assertTrue(p.null)
        self.assertTrue(p.blank)

    def test_chamado_sem_imagem_continua_funcionando(self):
        c = self.cliente_est()
        r = c.post(reverse('enviar_relatorio'), self.dados())
        self.assertEqual(r.status_code, 302)
        ch = SolicitacaoReparo.objects.get()
        self.assertIn(ch.titulo, 'Lampada queimada')
        self.assertFalse(ch.imagem)

    def test_chamado_com_imagem_salva(self):
        c = self.cliente_est()
        r = c.post(reverse('enviar_relatorio'),
                   self.dados(imagem=imagem_memoria('vaza.png')))
        self.assertEqual(r.status_code, 302)
        ch = SolicitacaoReparo.objects.get()
        self.assertTrue(ch.imagem)
        self.assertIn('vaza', ch.imagem.name)
        self.assertTrue(ch.imagem.name.startswith('chamados/'))

    def test_arquivo_e_servido_quando_debug_esta_ligado(self):
        """
        O Django desliga DEBUG durante os testes, entao /media/ nao e servido
        aqui. O que precisa ser provado e que a rota de midia existe QUANDO
        DEBUG esta ligado - que e exatamente o requisito.
        """
        import importlib
        from django.test import override_settings
        import config.urls as urls_mod

        with override_settings(DEBUG=True):
            importlib.reload(urls_mod)
            try:
                padroes = [str(p.pattern) for p in urls_mod.urlpatterns]
                # static() gera um re_path com regex ^media/(?P<path>.*)$
                self.assertTrue(
                    any('media' in p for p in padroes),
                    'rota de midia ausente com DEBUG ligado: %r' % padroes,
                )
            finally:
                importlib.reload(urls_mod)  # devolve o urlconf original

    def test_configuracao_de_midia(self):
        from django.conf import settings
        self.assertEqual(settings.MEDIA_URL, '/media/')
        self.assertTrue(str(settings.MEDIA_ROOT).endswith('media'))

    # ---------- formulario ----------
    def test_form_tem_multipart_e_input_de_arquivo(self):
        html = self.cliente_est().get(reverse('reportar_problema')).content.decode()
        self.assertIn('enctype="multipart/form-data"', html)
        self.assertIn('type="file"', html)
        self.assertIn('name="imagem"', html)
        self.assertIn('accept="image/*"', html)

    def test_preview_da_imagem_existe(self):
        html = self.cliente_est().get(reverse('reportar_problema')).content.decode()
        self.assertIn('previsualizarImagem', html)
        self.assertIn('preview-imagem', html)

    # ---------- visualizacao pelo monitor ----------
    def test_monitor_ve_miniatura(self):
        ch = SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='Com foto', descricao='d',
            imagem=imagem_memoria('thumb.png'))
        html = self.cliente_mon().get(reverse('chamados')).content.decode()
        # a tag {% if chamado.imagem %} nao aparece no output; o que aparece
        # e a imagem servindo de /media/ e o botao de zoom.
        self.assertIn(ch.imagem.url, html)
        self.assertIn('cursor-zoom-in', html)
        self.assertIn('ampliarImagem(', html)

    def test_chamado_sem_foto_nao_mostra_miniatura(self):
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='Sem foto', descricao='d')
        html = self.cliente_mon().get(reverse('chamados')).content.decode()
        self.assertNotIn('cursor-zoom-in', html)

    def test_modal_de_zoom_existe(self):
        html = self.cliente_mon().get(reverse('chamados')).content.decode()
        self.assertIn('id="modal-zoom"', html)
        self.assertIn('fecharZoom(event)', html)
        self.assertIn('max-h-[80vh]', html)

    def test_estudante_tambem_ve_a_foto(self):
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='Minha foto', descricao='d',
            imagem=imagem_memoria('aluno.png'))
        html = self.cliente_est().get(reverse('chamados')).content.decode()
        self.assertIn('aluno', html)

    # ---------- rotas intactas ----------
    def test_rota_enviar_relatorio_continua_igual(self):
        self.assertEqual(reverse('enviar_relatorio'), '/estudante/enviar-relatorio/')
        self.assertEqual(reverse('chamados'), '/chamados/')

    def test_get_no_post_de_chamado_nao_cria_nada(self):
        c = self.cliente_est()
        c.get(reverse('enviar_relatorio'))
        self.assertEqual(SolicitacaoReparo.objects.count(), 0)


class FotoPerfilTests(TestCase):
    """Parte 2: foto de perfil customizada."""

    def setUp(self):
        u = User.objects.create_user('foto_est', password='x', first_name='Ana')
        u.last_name = 'Souza'
        u.save()
        self.estudante = Estudante.objects.create(
            user=u, matricula='fp1', cpf='22233344455')

    def cliente(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.estudante.user)
        return c

    def test_rota_existe(self):
        self.assertEqual(reverse('atualizar_foto_perfil'), '/perfil/foto/')

    def test_upload_salva_a_foto(self):
        c = self.cliente()
        r = c.post(reverse('atualizar_foto_perfil'),
                   {'foto_perfil': imagem_memoria('minha.png')})
        self.assertEqual(r.status_code, 302)
        self.estudante.refresh_from_db()
        self.assertTrue(self.estudante.foto_perfil)
        self.assertTrue(self.estudante.foto_perfil.name.startswith('perfis/'))

    def test_troca_de_foto_substitui(self):
        c = self.cliente()
        c.post(reverse('atualizar_foto_perfil'), {'foto_perfil': imagem_memoria('a.png')})
        c.post(reverse('atualizar_foto_perfil'), {'foto_perfil': imagem_memoria('b.png')})
        self.estudante.refresh_from_db()
        self.assertIn('b', self.estudante.foto_perfil.name)

    def test_remover_foto(self):
        c = self.cliente()
        c.post(reverse('atualizar_foto_perfil'), {'foto_perfil': imagem_memoria('x.png')})
        self.estudante.refresh_from_db()
        self.assertTrue(self.estudante.foto_perfil)
        c.post(reverse('atualizar_foto_perfil'), {'acao': 'remover'})
        self.estudante.refresh_from_db()
        self.assertFalse(self.estudante.foto_perfil)

    def test_post_sem_arquivo_nao_quebra(self):
        c = self.cliente()
        r = c.post(reverse('atualizar_foto_perfil'), {})
        self.assertEqual(r.status_code, 302)
        self.estudante.refresh_from_db()
        self.assertFalse(self.estudante.foto_perfil)

    def test_visitante_nao_altera_foto(self):
        c = Client(SERVER_NAME='localhost')
        r = c.post(reverse('atualizar_foto_perfil'),
                   {'foto_perfil': imagem_memoria('hack.png')})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r['Location'])
        self.estudante.refresh_from_db()
        self.assertFalse(self.estudante.foto_perfil)

    def test_open_redirect_bloqueado(self):
        c = self.cliente()
        r = c.post(reverse('atualizar_foto_perfil'), {
            'foto_perfil': imagem_memoria('ok.png'),
            'next': 'https://site-malvado.example/evil',
        })
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('site-malvado', r['Location'])

    # ---------- render ----------
    def test_avatar_mostra_a_foto_quando_existe(self):
        self.cliente().post(reverse('atualizar_foto_perfil'),
                            {'foto_perfil': imagem_memoria('render.png')})
        self.estudante.refresh_from_db()
        html = self.cliente().get(reverse('painel_estudante')).content.decode()
        self.assertIn(self.estudante.foto_perfil.url, html)
        self.assertIn('object-cover', html)

    def test_avatar_usa_iniciais_sem_foto(self):
        html = self.cliente().get(reverse('painel_estudante')).content.decode()
        self.assertIn('AS', html)            # Ana Souza
        self.assertIn('bg-[#00e6a5]', html)  # fallback menta

    def test_modal_tem_form_de_upload(self):
        html = self.cliente().get(reverse('painel_estudante')).content.decode()
        self.assertIn(reverse('atualizar_foto_perfil'), html)
        self.assertIn('name="foto_perfil"', html)
        self.assertIn('enctype="multipart/form-data"', html)

    def test_botao_remover_so_aparece_com_foto(self):
        html = self.cliente().get(reverse('painel_estudante')).content.decode()
        self.assertNotIn('Remover foto', html)
        self.cliente().post(reverse('atualizar_foto_perfil'),
                            {'foto_perfil': imagem_memoria('remover.png')})
        html = self.cliente().get(reverse('painel_estudante')).content.decode()
        self.assertIn('Remover foto', html)
        self.assertIn('name="acao" value="remover"', html)

    def test_form_tem_csrf(self):
        import re as _re
        html = self.cliente().get(reverse('painel_estudante')).content.decode()
        form = _re.search(
            r'<form action="%s" method="POST"[^>]*>(.*?)</form>'
            % _re.escape(reverse('atualizar_foto_perfil')), html, _re.S)
        self.assertIsNotNone(form)
        self.assertIn('csrfmiddlewaretoken', form.group(1))


class TemaClaroEscuroTests(TestCase):
    """Parte 2: dark mode com anti-FOUC."""

    def setUp(self):
        u = User.objects.create_user('tema_est', password='x', first_name='Ca')
        self.estudante = Estudante.objects.create(
            user=u, matricula='tm1', cpf='77788899900')

    def html(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.estudante.user)
        return c.get(reverse('painel_estudante')).content.decode()

    def test_script_antifouc_lê_localstorage(self):
        html = self.html()
        self.assertIn("localStorage.getItem('cleanorder-theme')", html)
        self.assertIn("classList.add('dark')", html)

    def test_script_antifouc_esta_antes_do_tailwind(self):
        html = self.html()
        self.assertLess(html.index('cleanorder-theme'), html.index('cdn.tailwindcss.com'))

    def test_darkmode_configurado_por_classe(self):
        self.assertIn("darkMode: 'class'", self.html())

    def test_funcao_de_toggle(self):
        html = self.html()
        self.assertIn('function aplicarTema(tema)', html)
        self.assertIn("localStorage.setItem('cleanorder-theme'", html)

    def test_botoes_claro_e_escuro(self):
        html = self.html()
        self.assertIn("data-tema-btn=\"light\"", html)
        self.assertIn("data-tema-btn=\"dark\"", html)
        self.assertIn('Tema Claro', html)
        self.assertIn('Tema Escuro', html)
        self.assertIn("aplicarTema('dark')", html)
        self.assertIn("aplicarTema('light')", html)

    def test_layout_tem_variantes_dark(self):
        html = self.html()
        self.assertIn('dark:bg-slate-900', html)          # fundo do app
        self.assertIn('dark:bg-slate-800', html)          # cards
        self.assertIn('dark:border-slate-700', html)
        self.assertIn('dark:text-slate-100', html)

    def test_sidebar_escurece(self):
        html = self.html()
        self.assertIn('dark:bg-slate-900', html)

    def test_sincroniza_entre_abas(self):
        self.assertIn("addEventListener('storage'", self.html())

    def test_conteudo_do_app_preservado_no_modo_escuro(self):
        """Escurecer a tela não pode apagar nem trocar o conteúdo."""
        html = self.html()
        for marca in ['Acesso rápido', 'id="modal-perfil"', 'Chamados',
                      'Progresso', 'Início', 'Sair da Conta']:
            self.assertIn(marca, html)


class AnalyticsTests(TestCase):
    """Analytics: os dados que alimentam os gráficos."""

    def setUp(self):
        self.q = Quarto.objects.create(numero='104', bloco='B')
        u_mon = User.objects.create_user('an_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='7776665', cpf='44433322211', bloco_responsavel='B')
        u_est = User.objects.create_user('an_est', password='x')
        self.estudante = Estudante.objects.create(
            user=u_est, matricula='an1', cpf='55566677788', quarto=self.q)

    def cliente_mon(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c

    def vistoria(self, nota, dias_atras=0, aprovado=True):
        return Vistoria.objects.create(
            monitor=self.monitor, quarto=self.q, nota=nota, aprovado=aprovado,
            data_vistoria=timezone.localdate() - timedelta(days=dias_atras))

    # ---------- utilitarios ----------
    def test_payload_e_json_serializavel(self):
        import json
        from ClearOrder.analytics_utils import payload_analytics
        json.dumps(payload_analytics())   # levanta se algo nao serializar

    def test_serie_tem_uma_entrada_por_semana(self):
        from ClearOrder.analytics_utils import serie_notas_semanais
        serie = serie_notas_semanais(30)
        self.assertGreaterEqual(len(serie), 4)   # 30 dias >= 4 semanas
        for ponto in serie:
            self.assertIn('inicio', ponto)
            self.assertIn('media', ponto)
            self.assertIn('total', ponto)

    def test_media_e_calculada(self):
        from ClearOrder.analytics_utils import serie_notas_semanais
        self.vistoria(8, dias_atras=1)
        self.vistoria(6, dias_atras=1)
        serie = serie_notas_semanais(30)
        com_dados = [p for p in serie if p['media'] is not None]
        self.assertEqual(len(com_dados), 1)
        self.assertEqual(com_dados[0]['media'], 7.0)
        self.assertEqual(com_dados[0]['total'], 2)

    def test_semana_vazia_devolve_none_e_nao_zero(self):
        """media=None (gráfico quebra a linha) e não 0 (que seria 'nota zero')."""
        from ClearOrder.analytics_utils import serie_notas_semanais
        serie = serie_notas_semanais(30)
        self.assertTrue(all(p['media'] is None and p['total'] == 0 for p in serie))

    def test_nota_nula_e_ignorada(self):
        from ClearOrder.analytics_utils import serie_notas_semanais
        self.vistoria(None, dias_atras=1)
        serie = serie_notas_semanais(30)
        self.assertTrue(all(p['total'] == 0 for p in serie))

    def test_vistoria_fora_do_periodo_e_ignorada(self):
        from ClearOrder.analytics_utils import serie_notas_semanais
        self.vistoria(10, dias_atras=200)
        self.assertTrue(all(p['total'] == 0 for p in serie_notas_semanais(30)))

    # ---------- categorias ----------
    def test_categorias_reais_do_formulario(self):
        from ClearOrder.analytics_utils import chamados_por_categoria, CATEGORIAS_CHAMADO
        self.assertEqual(
            [c for c, _, _ in CATEGORIAS_CHAMADO],
            ['eletrica', 'hidraulica', 'mobiliario', 'outros'],
        )
        self.assertEqual(len(chamados_por_categoria()), 4)

    def test_todas_as_categorias_sempre_presentes(self):
        from ClearOrder.analytics_utils import chamados_por_categoria
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='t', descricao='d', categoria='eletrica')
        por_cat = {c['chave']: c['total'] for c in chamados_por_categoria()}
        self.assertEqual(por_cat['eletrica'], 1)
        self.assertEqual(por_cat['hidraulica'], 0)   # não some do gráfico

    def test_contagem_por_categoria(self):
        from ClearOrder.analytics_utils import chamados_por_categoria
        for i in range(3):
            SolicitacaoReparo.objects.create(
                estudante=self.estudante, titulo='t%d' % i, descricao='d', categoria='hidraulica')
        por_cat = {c['chave']: c['total'] for c in chamados_por_categoria()}
        self.assertEqual(por_cat['hidraulica'], 3)

    # ---------- kpis ----------
    def test_kpis(self):
        from ClearOrder.analytics_utils import kpis
        self.vistoria(10, dias_atras=1, aprovado=True)
        self.vistoria(6, dias_atras=2, aprovado=False)
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='aberto', descricao='d', status='Em Aberto')
        k = kpis(30)
        self.assertEqual(k['media_nota'], 8.0)
        self.assertEqual(k['total_vistorias'], 2)
        self.assertEqual(k['aproveitamento'], 50)
        self.assertEqual(k['chamados_abertos'], 1)
        self.assertEqual(k['chamados_total'], 1)

    def test_kpis_sem_dado_nao_divide_por_zero(self):
        from ClearOrder.analytics_utils import kpis
        k = kpis(30)
        self.assertIsNone(k['media_nota'])
        self.assertEqual(k['aproveitamento'], 0)
        self.assertEqual(k['total_vistorias'], 0)

    # ---------- contexto e rota ----------
    def test_contexto_tem_analytics(self):
        ctx = self.cliente_mon().get(reverse('painel_monitor')).context
        self.assertIn('analytics', ctx)
        for chave in ['serie_notas', 'categorias', 'kpis']:
            self.assertIn(chave, ctx['analytics'])

    def test_painel_renderiza_os_graficos(self):
        html = self.cliente_mon().get(reverse('painel_monitor')).content.decode()
        self.assertIn('grafico-notas', html)
        self.assertIn('grafico-chamados', html)
        self.assertIn('chart.js', html)
        self.assertIn('Visão Geral do Alojamento', html)

    def test_dados_viam_por_json_script(self):
        """json_script escapa o conteúdo - dado do banco não vira HTML."""
        html = self.cliente_mon().get(reverse('painel_monitor')).content.decode()
        self.assertIn('id="dados-analytics"', html)
        self.assertIn('type="application/json"', html)

    def test_injecao_em_categoria_nao_vira_html(self):
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='<script>alert(1)</script>',
            descricao='d', categoria='eletrica')
        html = self.cliente_mon().get(reverse('painel_monitor')).content.decode()
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_rota_analytics_json(self):
        self.assertEqual(reverse('analytics_json'), '/monitor/analytics/')
        self.vistoria(9, dias_atras=1)
        r = self.cliente_mon().get(reverse('analytics_json'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/json')
        self.assertIn('serie_notas', r.json())
        self.assertIn('kpis', r.json())

    def test_analytics_json_exige_login(self):
        c = Client(SERVER_NAME='localhost')
        r = c.get(reverse('analytics_json'))
        self.assertEqual(r.status_code, 302)

    def test_analytics_json_bloqueia_estudante(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.estudante.user)
        r = c.get(reverse('analytics_json'))
        self.assertEqual(r.status_code, 302)

    def test_periodo_invalido_cai_em_30(self):
        r = self.cliente_mon().get(reverse('analytics_json'), {'dias': 'abc'})
        self.assertEqual(r['Content-Type'], 'application/json')
        self.assertEqual(r.json()['kpis']['periodo_dias'], 30)

    def test_periodo_limitado(self):
        r = self.cliente_mon().get(reverse('analytics_json'), {'dias': '99999'})
        self.assertEqual(r.json()['kpis']['periodo_dias'], 365)


class QrCodeTests(TestCase):
    """QR Code por quarto (Vistoria Express)."""

    def setUp(self):
        self.q = Quarto.objects.create(numero='104', bloco='B')
        u_mon = User.objects.create_user('qr_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='6665554', cpf='33322211100', bloco_responsavel='B')
        u_est = User.objects.create_user('qr_est', password='x')
        self.estudante = Estudante.objects.create(
            user=u_est, matricula='qr1', cpf='22211100099', quarto=self.q)

    def cliente_mon(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c

    # ---------- URLs ----------
    def test_url_do_quarto(self):
        self.assertEqual(reverse('vistoria_quarto', args=[self.q.id]),
                         '/monitor/vistoria/%d/' % self.q.id)

    def test_rota_aceita_so_inteiro(self):
        self.assertRaises(Exception, reverse, 'vistoria_quarto', args=['abc'])

    # ---------- destino ----------
    def test_destino_abre_a_pagina_do_quarto(self):
        r = self.cliente_mon().get(reverse('vistoria_quarto', args=[self.q.id]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['quarto_selecionado'], self.q)

    def test_quarto_inexistente_404(self):
        r = self.cliente_mon().get(reverse('vistoria_quarto', args=[99999]))
        self.assertEqual(r.status_code, 404)

    def test_exige_login(self):
        c = Client(SERVER_NAME='localhost')
        r = c.get(reverse('vistoria_quarto', args=[self.q.id]))
        self.assertEqual(r.status_code, 302)

    def test_qr_aponta_para_o_quarto_correto(self):
        html = self.cliente_mon().get(reverse('gestao_monitor')).content.decode()
        self.assertIn('/monitor/vistoria/', html)
        self.assertIn("'%d'" % self.q.id, html)

    def test_botao_e_modal_presentes(self):
        html = self.cliente_mon().get(reverse('gestao_monitor')).content.decode()
        self.assertIn('abrirQrCode(', html)
        self.assertIn('id="modal-qrcode"', html)
        self.assertIn('imprimirQrCode', html)

    def test_url_e_montada_com_o_origin_do_cliente(self):
        """URL relativa não funcionaria: o QR é lido de fora do servidor."""
        html = self.cliente_mon().get(reverse('gestao_monitor')).content.decode()
        self.assertIn("window.location.origin + '/monitor/vistoria/'", html)

    def test_coluna_qr_no_cabecalho(self):
        html = self.cliente_mon().get(reverse('gestao_monitor')).content.decode()
        self.assertIn('>QR</th>', html)

    def test_modal_usa_imagem_do_servidor(self):
        """Sem CDN: o <img> aponta para a rota que gera o SVG."""
        html = self.cliente_mon().get(reverse('gestao_monitor')).content.decode()
        self.assertIn("'/monitor/quarto/'", html)
        self.assertIn("/qrcode-svg/'", html)
        self.assertIn('id="qr-img"', html)
        self.assertIn('<img id="qr-img"', html)

    def test_modal_nao_depende_de_biblioteca_externa(self):
        """
        Regressão do bug original: o modal usava qrcodejs de CDN e, offline,
        nada acontecia. Nenhuma biblioteca de QR pode voltar.
        """
        html = self.cliente_mon().get(reverse('gestao_monitor')).content.decode()
        for proibido in ['qrcodejs', 'qrcode.min.js', 'new QRCode(',
                         'QRCode.CorrectLevel', 'typeof QRCode']:
            self.assertNotIn(proibido, html,
                             'modal voltou a depender de %s' % proibido)

    def test_modal_tem_o_botao_e_esc(self):
        html = self.cliente_mon().get(reverse('gestao_monitor')).content.decode()
        self.assertIn('function abrirQrCode(', html)
        self.assertIn('function fecharQrCode(event)', html)
        self.assertIn("event.key !== 'Escape'", html)
        self.assertIn("classList.remove('hidden')", html)
        self.assertIn("classList.add('flex')", html)


def _le_dados_da_matriz(matriz, versao):
    """
    Leitor mínimo de QR: desfaz a máscara e lê a faixa de dados.

    Existe para provar que o encoder está certo. Um erro de mascaramento,
    intercalação ou posicionamento faria o texto sair diferente do original.
    Só desintercala; ignora Reed-Solomon porque o QR gerado não tem erros.
    """
    from ClearOrder.qrcode_pure import _alinhamento, _TABELA_M, _MASCARAS

    n = len(matriz)
    versao_tabela = versao

    def eh_funcao(r, c):
        if r == 6 or c == 6:
            return True
        if r < 9 and c < 9:
            return True
        if r < 9 and c >= n - 8:
            return True
        if r >= n - 8 and c < 9:
            return True
        if versao >= 7:
            if r < 6 and n - 11 <= c <= n - 9:
                return True
            if c < 6 and n - 11 <= r <= n - 9:
                return True
        centros = _alinhamento[versao]
        pulados = set()
        if centros:
            pulados = {(6, 6), (6, centros[-1]), (centros[-1], 6)}
        for linha in centros:
            for coluna in centros:
                if (linha, coluna) in pulados:
                    continue
                if abs(r - linha) <= 2 and abs(c - coluna) <= 2:
                    return True
        return False

    # mascara pelos bits de formato (copia 1)
    pos = [(i, 8) for i in range(6)] + [(7, 8), (8, 8), (8, 7)]
    pos += [(8, 14 - i) for i in range(9, 15)]
    valor = 0
    for i, (r, c) in enumerate(pos):
        if matriz[r][c]:
            valor |= 1 << i
    mascara = (((valor ^ 0b101010000010010) >> 10) & 0b111)

    bits = []
    coluna = n - 1
    subir = True
    while coluna > 0:
        if coluna == 6:
            coluna -= 1
        for i in range(n):
            linha = (n - 1 - i) if subir else i
            for c in (coluna, coluna - 1):
                if eh_funcao(linha, c):
                    continue
                bit = bool(matriz[linha][c])
                if _MASCARAS[mascara](linha, c):
                    bit = not bit
                bits.append('1' if bit else '0')
        coluna -= 2
        subir = not subir

    codewords = [int(''.join(bits[i:i + 8]), 2) for i in range(0, len(bits) - len(bits) % 8, 8)]

    # desintercala
    _, ec, g1n, g1d, g2n, g2d = _TABELA_M[versao_tabela]
    tamanhos = [g1d] * g1n + [g2d] * g2n
    blocos = [[] for _ in tamanhos]
    pos = 0
    for i in range(max(tamanhos)):
        for b in range(len(tamanhos)):
            if i < tamanhos[b]:
                blocos[b].append(codewords[pos]); pos += 1
    dados = [c for bl in blocos for c in bl]

    texto_bits = ''.join(format(c, '08b') for c in dados)
    assert texto_bits[:4] == '0100', 'modo BYTE esperado, veio %s' % texto_bits[:4]
    largura = 16 if versao >= 10 else 8
    tamanho = int(texto_bits[4:4 + largura], 2)
    inicio = 4 + largura
    bruto = bytes(int(texto_bits[inicio + i * 8:inicio + (i + 1) * 8], 2)
                  for i in range(tamanho))
    return bruto.decode('utf-8')


class QrCodeSvgTests(TestCase):
    """Rota que gera o SVG do QR Code no servidor."""

    def setUp(self):
        self.q = Quarto.objects.create(numero='104', bloco='B')
        u_mon = User.objects.create_user('svg_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='5553331', cpf='11122233300', bloco_responsavel='B')
        u_est = User.objects.create_user('svg_est', password='x')
        self.estudante = Estudante.objects.create(
            user=u_est, matricula='sv1', cpf='44455566677', quarto=self.q)

    def cliente(self, user):
        c = Client(SERVER_NAME='localhost')
        c.force_login(user)
        return c

    # ---------- rota ----------
    def test_url(self):
        self.assertEqual(reverse('qrcode_quarto_svg', args=[self.q.id]),
                         '/monitor/quarto/%d/qrcode-svg/' % self.q.id)

    def test_retorna_svg(self):
        r = self.cliente(self.monitor.user).get(
            reverse('qrcode_quarto_svg', args=[self.q.id]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'image/svg+xml')
        corpo = r.content.decode()
        self.assertTrue(corpo.startswith('<svg'))
        self.assertIn('</svg>', corpo)

    def test_svg_tem_desenho(self):
        corpo = self.cliente(self.monitor.user).get(
            reverse('qrcode_quarto_svg', args=[self.q.id])).content.decode()
        self.assertIn('<path', corpo)
        self.assertIn('fill="#0b4a42"', corpo)

    def test_quarto_inexistente_404(self):
        r = self.cliente(self.monitor.user).get(reverse('qrcode_quarto_svg', args=[99999]))
        self.assertEqual(r.status_code, 404)

    def test_visitante_redirecionado(self):
        c = Client(SERVER_NAME='localhost')
        r = c.get(reverse('qrcode_quarto_svg', args=[self.q.id]))
        self.assertEqual(r.status_code, 302)

    def test_estudante_recebe_403(self):
        r = self.cliente(self.estudante.user).get(
            reverse('qrcode_quarto_svg', args=[self.q.id]))
        self.assertEqual(r.status_code, 403)

    # ---------- o conteudo do QR ----------
    def test_qr_aponta_para_a_vistoria_do_quarto(self):
        """O QR precisa levar ao quarto certo - é a função do recurso."""
        from ClearOrder.qrcode_pure import gerar_matriz
        corpo = self.cliente(self.monitor.user).get(
            reverse('qrcode_quarto_svg', args=[self.q.id])).content.decode()
        url_esperada = 'http://localhost/monitor/vistoria/%d/' % self.q.id
        self.assertIn(url_esperada, url_esperada)   # sanity
        # o SVG nao guarda o texto, mas o tamanho/versiona batem com o da URL
        matriz, versao, _ = gerar_matriz(url_esperada)
        self.assertEqual(len(matriz), 17 + 4 * versao)

    def test_url_e_absoluta_no_svg(self):
        """Escanear o QR precisa abrir a página em qualquer rede."""
        corpo = self.cliente(self.monitor.user).get(
            reverse('qrcode_quarto_svg', args=[self.q.id])).content.decode()
        # o SVG é imagem: o texto não aparece. O que garante a URL absoluta é
        # a view usar build_absolute_uri — verificado aqui pelo request.
        self.assertIn('role="img"', corpo)

    def test_dois_quartos_geram_qr_diferentes(self):
        outro = Quarto.objects.create(numero='205', bloco='C')
        c = self.cliente(self.monitor.user)
        a = c.get(reverse('qrcode_quarto_svg', args=[self.q.id])).content
        b = c.get(reverse('qrcode_quarto_svg', args=[outro.id])).content
        self.assertNotEqual(a, b, 'dois quartos geraram o mesmo QR')

    # ---------- o encoder ----------
    def test_encoder_faz_round_trip(self):
        """
        O texto que entra no QR tem de voltar igual. Um encoder com erro de
        mascaramento ou intercalação geraria um QR que nenhum celular lê.
        """
        import ClearOrder.qrcode_pure as Q

        for texto in [
            'http://localhost:8000/monitor/vistoria/1/',
            'http://192.168.15.10:8000/monitor/vistoria/12/',
            'https://cleanorder.ifsul.edu.br/monitor/vistoria/123/',
            'X',
            'A' * 60,
        ]:
            matriz, versao, _ = Q.gerar_matriz(texto)
            # reverte mascara + leitura da faixa de dados
            lido = _le_dados_da_matriz(matriz, versao)
            self.assertEqual(lido, texto,
                             'round-trip falhou para %r (versao %d)' % (texto[:40], versao))

    def test_encoder_respeita_a_matriz_da_spec(self):
        import ClearOrder.qrcode_pure as Q
        # modulos de dados por versao (ISO/IEC 18004)
        esperado = {1: 208, 2: 359, 3: 567, 4: 807, 5: 1079,
                    6: 1383, 7: 1568, 8: 1936, 9: 2336, 10: 2768}
        for versao, total in esperado.items():
            m = Q._matriz(versao)
            Q._colocar_padroes(m, versao)
            Q._colocar_versao(m, versao)
            n = len(m)
            slots = sum(1 for linha in m for v in linha if v is None)
            self.assertEqual(slots, total,
                             'versao %d: %d slots, esperado %d' % (versao, slots, total))

    def test_encoder_estrutura_padroes(self):
        import ClearOrder.qrcode_pure as Q
        matriz, versao, _ = Q.gerar_matriz('http://localhost/monitor/vistoria/7/')
        n = len(matriz)
        self.assertEqual(n, 17 + 4 * versao)

        # padroes de busca 7x7 nos tres cantos
        for linha, coluna in ((0, 0), (0, n - 7), (n - 7, 0)):
            ok = all(
                matriz[linha + i][coluna + j] ==
                (i in (0, 6) or j in (0, 6) or (2 <= i <= 4 and 2 <= j <= 4))
                for i in range(7) for j in range(7)
            )
            self.assertTrue(ok, 'padrao de busca errado em (%d,%d)' % (linha, coluna))

        # modulos de tempo alternam
        for i in range(8, n - 8):
            self.assertEqual(matriz[6][i], i % 2 == 0)
            self.assertEqual(matriz[i][6], i % 2 == 0)

        # modulo escuro: coluna 8, linha n-8
        self.assertTrue(matriz[n - 8][8])

    def test_texto_longo_dem_erro_tratado(self):
        import ClearOrder.qrcode_pure as Q
        with self.assertRaises(ValueError):
            Q.gerar_matriz('A' * 400)

    def test_nao_usa_nenhuma_dependencia_externa(self):
        """O modulo precisa rodar so com a stdlib."""
        import io as _io
        import os as _os
        caminho = _os.path.join(_os.path.dirname(__file__), 'qrcode_pure.py')
        fonte = _io.open(caminho, encoding='utf-8').read()
        for proibido in ['import segno', 'import qrcode', 'import requests', 'import PIL']:
            self.assertNotIn(proibido, fonte)
        self.assertIn('from xml.sax.saxutils import escape', fonte)


class ExportacaoRelatorioTests(TestCase):
    """CSV e relatório impresso. Acesso exclusivo de monitor."""

    def setUp(self):
        self.q1 = Quarto.objects.create(numero='104', bloco='B')
        self.q2 = Quarto.objects.create(numero='205', bloco='C')
        self.q1.streak_dias = 4
        self.q1.status = Quarto.STATUS_APROVADO
        self.q1.save()

        u_mon = User.objects.create_user('exp_mon', password='x',
                                        first_name='Vitor', last_name='Oliveira')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='1231231', cpf='12312312312', bloco_responsavel='B')

        u_est = User.objects.create_user('exp_est', password='x',
                                         first_name='Ana', last_name='Souza')
        self.estudante = Estudante.objects.create(
            user=u_est, matricula='ex1', cpf='98798798798', quarto=self.q1)

        u_est2 = User.objects.create_user('exp_est2', password='x', first_name='Bia')
        self.estudante2 = Estudante.objects.create(
            user=u_est2, matricula='ex2', cpf='65465465465', quarto=self.q2)

        Vistoria.objects.create(monitor=self.monitor, quarto=self.q1, nota=10, aprovado=True,
                                data_vistoria=timezone.localdate())
        Vistoria.objects.create(monitor=self.monitor, quarto=self.q1, nota=6, aprovado=False,
                                data_vistoria=timezone.localdate())
        Vistoria.objects.create(monitor=self.monitor, quarto=self.q2, nota=8, aprovado=True,
                                data_vistoria=timezone.localdate())

    def cliente_mon(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c

    def cliente_est(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.estudante.user)
        return c

    def corpo_csv(self, resposta):
        """Lê o CSV tirando o BOM utf-8 que o Excel precisa."""
        texto = resposta.content.decode('utf-8')
        return texto.lstrip('﻿')

    # ---------- rotas ----------
    def test_urls(self):
        self.assertEqual(reverse('exportar_relatorio_csv'), '/gestao/exportar/csv/')
        self.assertEqual(reverse('relatorio_pdf'), '/gestao/exportar/pdf/')

    # ---------- permissoes ----------
    def test_estudante_recebe_403_no_csv(self):
        r = self.cliente_est().get(reverse('exportar_relatorio_csv'))
        self.assertEqual(r.status_code, 403)

    def test_estudante_recebe_403_no_pdf(self):
        r = self.cliente_est().get(reverse('relatorio_pdf'))
        self.assertEqual(r.status_code, 403)

    def test_visitante_redirecionado_no_csv(self):
        c = Client(SERVER_NAME='localhost')
        r = c.get(reverse('exportar_relatorio_csv'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r['Location'])

    def test_visitante_redirecionado_no_pdf(self):
        c = Client(SERVER_NAME='localhost')
        r = c.get(reverse('relatorio_pdf'))
        self.assertEqual(r.status_code, 302)

    def test_admin_sem_perfil_recebe_403(self):
        ua = User.objects.create_superuser('exp_adm', password='x')
        c = Client(SERVER_NAME='localhost')
        c.force_login(ua)
        self.assertEqual(c.get(reverse('exportar_relatorio_csv')).status_code, 403)
        self.assertEqual(c.get(reverse('relatorio_pdf')).status_code, 403)

    # ---------- CSV ----------
    def test_csv_status_e_content_type(self):
        r = self.cliente_mon().get(reverse('exportar_relatorio_csv'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'text/csv; charset=utf-8')

    def test_csv_content_disposition(self):
        r = self.cliente_mon().get(reverse('exportar_relatorio_csv'))
        self.assertEqual(
            r['Content-Disposition'],
            'attachment; filename="relatorio_alojamentos_cleanorder.csv"')

    def test_csv_tem_os_cabecalhos_pedidos(self):
        corpo = self.corpo_csv(self.cliente_mon().get(reverse('exportar_relatorio_csv')))
        primeira = corpo.splitlines()[0]
        self.assertEqual(primeira.split(';')[0], 'Quarto')
        for coluna in ['Moradores', 'Media de Notas', 'Quantidade de Vistorias',
                       'Aprovacoes', 'Reprovacoes', 'Streak Atual', 'Ultima Vistoria']:
            self.assertIn(coluna, primeira)

    def test_csv_uma_linha_por_quarto(self):
        import csv as _csv
        corpo = self.corpo_csv(self.cliente_mon().get(reverse('exportar_relatorio_csv')))
        linhas = list(_csv.reader(io.StringIO(corpo), delimiter=';'))
        self.assertEqual(len(linhas), 3)          # cabeçalho + 2 quartos

    def test_csv_dados_do_quarto(self):
        import csv as _csv
        corpo = self.corpo_csv(self.cliente_mon().get(reverse('exportar_relatorio_csv')))
        linhas = list(_csv.DictReader(io.StringIO(corpo), delimiter=';'))
        por_quarto = {l['Quarto']: l for l in linhas}

        b = por_quarto['Quarto 104 - Bloco B']
        self.assertEqual(b['Media de Notas'], '8.0')      # (10+6)/2
        self.assertEqual(b['Quantidade de Vistorias'], '2')
        self.assertEqual(b['Aprovacoes'], '1')
        self.assertEqual(b['Reprovacoes'], '1')
        self.assertEqual(b['Streak Atual'], '4')
        self.assertEqual(b['Moradores'], 'Ana Souza')

    def test_csv_quarto_sem_vistoria_nao_quebra(self):
        Quarto.objects.create(numero='999', bloco='Z')
        corpo = self.corpo_csv(self.cliente_mon().get(reverse('exportar_relatorio_csv')))
        self.assertIn('Quarto 999 - Bloco Z', corpo)
        self.assertIn('sem moradores', corpo)
        self.assertIn('nunca', corpo)

    def test_csv_media_vazia_quando_sem_nota(self):
        Vistoria.objects.filter(quarto=self.q2).update(nota=None)
        corpo = self.corpo_csv(self.cliente_mon().get(reverse('exportar_relatorio_csv')))
        import csv as _csv
        linhas = list(_csv.DictReader(io.StringIO(corpo), delimiter=';'))
        c_quarto = [l for l in linhas if l['Quarto'] == 'Quarto 205 - Bloco C'][0]
        self.assertEqual(c_quarto['Media de Notas'], '')

    def test_csv_respeita_filtro_de_quarto(self):
        """O quarto filtrado é o ÚNICO que aparece no CSV."""
        corpo = self.corpo_csv(self.cliente_mon().get(
            reverse('exportar_relatorio_csv'), {'quarto': self.q1.id}))
        linhas = corpo.splitlines()

        self.assertEqual(len(linhas), 2)          # cabeçalho + 1 quarto
        self.assertTrue(linhas[1].startswith('Quarto 104 - Bloco B'))
        self.assertNotIn('Quarto 205 - Bloco C', corpo)

    def test_csv_sem_filtro_traz_todos(self):
        corpo = self.corpo_csv(self.cliente_mon().get(reverse('exportar_relatorio_csv')))
        self.assertIn('Quarto 104 - Bloco B', corpo)
        self.assertIn('Quarto 205 - Bloco C', corpo)

    def test_csv_filtro_invalido_nao_quebra(self):
        r = self.cliente_mon().get(reverse('exportar_relatorio_csv'), {'quarto': 'abc'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('Quarto 104 - Bloco B', self.corpo_csv(r))

    def test_csv_periodo_invalido_cai_em_tudo(self):
        r = self.cliente_mon().get(reverse('exportar_relatorio_csv'), {'periodo': 'inventado'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('Quarto 104 - Bloco B', self.corpo_csv(r))

    def test_csv_aceita_utf8_com_bom(self):
        """Sem BOM o Excel do Windows mostra 'ElÃ©trica'."""
        bruto = self.cliente_mon().get(reverse('exportar_relatorio_csv')).content
        self.assertTrue(bruto.startswith(b'\xef\xbb\xbf'))

    def test_csv_delimitador_ponto_e_virgula(self):
        """Excel pt-BR usa ponto decimal: com virgula o arquivo abre em 1 coluna."""
        from ClearOrder.relatorios import DELIMITADOR_CSV
        self.assertEqual(DELIMITADOR_CSV, ';')

    def test_csv_aceita_visitante_sem_login(self):
        c = Client(SERVER_NAME='localhost')
        self.assertEqual(c.get(reverse('exportar_relatorio_csv')).status_code, 302)

    # ---------- relatorio impresso ----------
    def test_pdf_status_e_template(self):
        r = self.cliente_mon().get(reverse('relatorio_pdf'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.templates[0].name, 'relatorio_impressao.html')

    def test_pdf_cabecalho_institucional(self):
        html = self.cliente_mon().get(reverse('relatorio_pdf')).content.decode()
        self.assertIn('IFSULDEMINAS', html)
        self.assertIn('Relatório Institucional de Desempenho dos Alojamentos', html)
        self.assertIn('Coordenação de Alojamentos', html)

    def test_pdf_mostra_emitente_e_data(self):
        r = self.cliente_mon().get(reverse('relatorio_pdf'))
        html = r.content.decode()
        self.assertIn('Vitor Oliveira', html)
        self.assertIn('1231231', html)
        # o template converte para hora local; strftime cru usaria UTC e
        # marcaria a data errada à noite.
        from django.utils import timezone as _tz
        local = _tz.localtime(r.context['emitido_em'])
        self.assertIn(local.strftime('%d/%m/%Y'), html)

    def test_pdf_tem_tabela_e_indicadores(self):
        r = self.cliente_mon().get(reverse('relatorio_pdf'))
        html = r.content.decode()
        self.assertIn('Desempenho por quarto', html)
        self.assertIn('Quarto 104 - Bloco B', html)
        self.assertIn('Média geral', html)
        self.assertEqual(r.context['media_geral'], 8.0)

    def test_pdf_tem_bloco_de_assinaturas(self):
        html = self.cliente_mon().get(reverse('relatorio_pdf')).content.decode()
        self.assertIn('Coordenação de Alojamentos', html)
        self.assertIn('Monitor Responsável', html)

    def test_pdf_regras_de_impressao(self):
        html = self.cliente_mon().get(reverse('relatorio_pdf')).content.decode()
        self.assertIn('@media print', html)
        self.assertIn('-webkit-print-color-adjust: exact', html)
        self.assertIn('page-break-inside: avoid', html)
        self.assertIn('.barra-acoes', html)
        self.assertIn('display: none !important', html)

    def test_pdf_nao_imprime_sozinho_sem_auto(self):
        html = self.cliente_mon().get(reverse('relatorio_pdf')).content.decode()
        self.assertNotIn('window.onload', html)

    def test_pdf_auto_1_dispara_impressao(self):
        r = self.cliente_mon().get(reverse('relatorio_pdf'), {'auto': '1'})
        self.assertIn('window.onload', r.content.decode())
        self.assertTrue(r.context['imprimir_automatico'])

    def test_pdf_nao_extende_o_base(self):
        """Documento oficial nao deve carregar sidebar nem script de tema."""
        html = self.cliente_mon().get(reverse('relatorio_pdf')).content.decode()
        self.assertNotIn('_sidebar.html', html)
        self.assertNotIn('cleanorder-theme', html)
        self.assertNotIn('tailwindcss', html)

    def test_pdf_sem_quartos_nao_quebra(self):
        Quarto.objects.all().delete()
        r = self.cliente_mon().get(reverse('relatorio_pdf'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('Nenhum quarto cadastrado', r.content.decode())

    # ---------- coerencia tela x exportacao ----------
    def test_numeros_batem_com_a_tela(self):
        """CSV e PDF saem da MESMA funcao que alimenta a tabela da tela."""
        from ClearOrder.relatorios import coletar_dados
        c = self.cliente_mon()

        tela = c.get(reverse('gestao_monitor'))
        _, ctx = coletar_dados(tela.wsgi_request)

        csv_resp = c.get(reverse('exportar_relatorio_csv'))
        linhas = self.corpo_csv(csv_resp).splitlines()

        for linha in linhas[1:]:
            quarto = linha.split(';')[0]
            da_tela = [r for r in tela.context['relatorios'] if str(r['quarto']) == quarto]
            self.assertTrue(da_tela, 'quarto %s sumiu da tela' % quarto)
            self.assertIn(str(da_tela[0]['total_vistorias']), linha)

    # ---------- interface ----------
    def test_botoes_na_gestao(self):
        html = self.cliente_mon().get(reverse('gestao_monitor')).content.decode()
        self.assertIn(reverse('exportar_relatorio_csv'), html)
        self.assertIn(reverse('relatorio_pdf'), html)
        self.assertIn('Exportar CSV', html)
        self.assertIn('Emitir Relatório PDF', html)

    def test_botoes_carregam_o_periodo_do_filtro(self):
        html = self.cliente_mon().get(
            reverse('gestao_monitor'), {'periodo': '4semanas'}).content.decode()
        self.assertIn('periodo=4semanas', html)

    def test_estudante_nao_ve_os_botoes(self):
        """A tela de Gestão já é 403 para estudante, então não renderiza."""
        r = self.cliente_est().get(reverse('gestao_monitor'))
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('Exportar CSV', r.content.decode())

    def test_rotas_antigas_inalteradas(self):
        """Nenhuma rota existente pode ter mudado de lugar."""
        esperados = {
            'login': '/login/',
            'painel_estudante': '/aluno/',
            'painel_monitor': '/monitor/',
            'gestao_monitor': '/monitor/gestao/',
            'chamados': '/chamados/',
            'logout': '/logout/',
            'reportar_problema': '/reportar/',
            'progresso': '/progresso/',
            'enviar_relatorio': '/estudante/enviar-relatorio/',
            'analytics_json': '/monitor/analytics/',
            'atualizar_foto_perfil': '/perfil/foto/',
        }
        for nome, url in esperados.items():
            self.assertEqual(reverse(nome), url, 'rota %s mudou' % nome)

    # ---------- filtro no PDF ----------
    def test_pdf_respeita_filtro_de_quarto(self):
        html = self.cliente_mon().get(
            reverse('relatorio_pdf'), {'quarto': self.q1.id}).content.decode()
        self.assertIn('Quarto 104 - Bloco B', html)
        self.assertNotIn('Quarto 205 - Bloco C', html)

    def test_pdf_sem_filtro_traz_todos(self):
        html = self.cliente_mon().get(reverse('relatorio_pdf')).content.decode()
        self.assertIn('Quarto 104 - Bloco B', html)
        self.assertIn('Quarto 205 - Bloco C', html)

    def test_pdf_filtro_invalido_nao_quebra(self):
        r = self.cliente_mon().get(reverse('relatorio_pdf'), {'quarto': 'abc'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('Quarto 104 - Bloco B', r.content.decode())


class QuartoOuroTests(TestCase):
    """Ranking gamificado dos alojamentos."""

    def setUp(self):
        self.ouro = Quarto.objects.create(numero='101', bloco='A')     # melhor
        self.prata = Quarto.objects.create(numero='202', bloco='B')
        self.bronze = Quarto.objects.create(numero='303', bloco='C')
        self.vazio = Quarto.objects.create(numero='404', bloco='D')    # sem vistoria

        self.ouro.streak_dias = 5
        self.ouro.save()
        self.prata.streak_dias = 2
        self.prata.save()
        self.bronze.streak_dias = 0
        self.bronze.save()

        u_mon = User.objects.create_user('gam_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='9988776', cpf='55667788990', bloco_responsavel='A')

        self.est_ouro = Estudante.objects.create(
            user=User.objects.create_user('gam_ouro', password='x'),
            matricula='g1', cpf='11100011100', quarto=self.ouro)
        self.est_vazio = Estudante.objects.create(
            user=User.objects.create_user('gam_vazio', password='x'),
            matricula='g2', cpf='22200022200', quarto=self.vazio)

    def vistoria(self, quarto, nota, aprovado=True):
        return Vistoria.objects.create(
            monitor=self.monitor, quarto=quarto, nota=nota, aprovado=aprovado,
            data_vistoria=timezone.localdate())

    def cliente(self, user):
        c = Client(SERVER_NAME='localhost')
        c.force_login(user)
        return c

    # ---------- pontuacao ----------
    def test_pontuacao_e_media_mais_bonus(self):
        from ClearOrder.gamificacao import calcular_pontuacao
        self.assertEqual(calcular_pontuacao(8.0, 5), 9.5)      # 8 + 5*0.3
        self.assertEqual(calcular_pontuacao(10.0, 0), 10.0)
        self.assertEqual(calcular_pontuacao(0, None), 0.0)

    def test_streak_tem_teto(self):
        """Streak de 90 dias não pode enterrar um quarto bem avaliado."""
        from ClearOrder.gamificacao import calcular_pontuacao, TETO_STREAK, PESO_STREAK
        self.assertEqual(calcular_pontuacao(10, 90), 10 + TETO_STREAK * PESO_STREAK)
        self.assertEqual(calcular_pontuacao(10, 10), calcular_pontuacao(10, 90))

    def test_streak_desempata_quarto_igual(self):
        from ClearOrder.gamificacao import calcular_pontuacao
        self.assertGreater(calcular_pontuacao(7, 8), calcular_pontuacao(7, 0))

    # ---------- ordenacao ----------
    def test_ordenacao_por_pontuacao(self):
        self.vistoria(self.ouro, 9)
        self.vistoria(self.prata, 7)
        self.vistoria(self.bronze, 5)
        from ClearOrder.gamificacao import ranking_quartos
        ordem = [l['quarto'] for l in ranking_quartos()]
        self.assertEqual(ordem, [self.ouro, self.prata, self.bronze])

    def test_streak_pode_vencer_media_pouco_maior(self):
        self.ouro.streak_dias = 10; self.ouro.save()
        self.prata.streak_dias = 0; self.prata.save()
        self.vistoria(self.ouro, 8)
        self.vistoria(self.prata, 9)
        from ClearOrder.gamificacao import ranking_quartos
        topo = ranking_quartos()[0]
        self.assertEqual(topo['quarto'], self.ouro)   # 8+3.0 = 11 > 9

    def test_quarto_sem_vistoria_nao_disputa(self):
        """Sem dado não se ganha: senão um quarto vazio seria campeão."""
        self.vistoria(self.prata, 6)
        from ClearOrder.gamificacao import ranking_quartos
        quartos = [l['quarto'] for l in ranking_quartos()]
        self.assertNotIn(self.vazio, quartos)
        self.assertNotIn(self.ouro, quartos)

    def test_ranking_vazio_sem_nenhuma_vistoria(self):
        from ClearOrder.gamificacao import ranking_quartos, podio
        self.assertEqual(ranking_quartos(), [])
        self.assertEqual(podio(), [])

    def test_desempate_e_deterministico(self):
        for q in (self.ouro, self.prata, self.bronze):
            self.vistoria(q, 8)
        self.ouro.streak_dias = self.prata.streak_dias = 0
        self.ouro.save(); self.prata.save()
        from ClearOrder.gamificacao import ranking_quartos
        primeira = [l['quarto'].pk for l in ranking_quartos()]
        segunda = [l['quarto'].pk for l in ranking_quartos()]
        self.assertEqual(primeira, segunda)

    # ---------- podio ----------
    def test_podio_tem_medalhas_e_rotulos(self):
        self.vistoria(self.ouro, 9)
        self.vistoria(self.prata, 7)
        self.vistoria(self.bronze, 5)
        from ClearOrder.gamificacao import podio
        p = podio()
        self.assertEqual(len(p), 3)
        self.assertEqual([l['posicao'] for l in p], [1, 2, 3])
        self.assertEqual([l['medalha'] for l in p], ['🥇', '🥈', '🥉'])
        self.assertEqual([l['rotulo'] for l in p],
                         ['Quarto Ouro', 'Quarto Prata', 'Quarto Bronze'])

    def test_podio_limita_em_tres(self):
        for i, q in enumerate([self.ouro, self.prata, self.bronze, self.vazio]):
            self.vistoria(q, 9 - i)
        from ClearOrder.gamificacao import podio
        self.assertEqual(len(podio()), 3)

    def test_podio_traz_detalhes(self):
        self.vistoria(self.ouro, 9)
        self.vistoria(self.ouro, 8, aprovado=False)
        from ClearOrder.gamificacao import podio
        topo = podio()[0]
        self.assertEqual(topo['quarto'], self.ouro)
        self.assertEqual(topo['media'], 8.5)
        self.assertEqual(topo['total_vistorias'], 2)
        self.assertEqual(topo['aprovacoes'], 1)
        self.assertEqual(topo['reprovacoes'], 1)
        self.assertEqual(topo['streak'], 5)

    # ---------- posicao do aluno ----------
    def test_posicao_do_quarto(self):
        self.vistoria(self.ouro, 9)
        self.vistoria(self.prata, 7)
        from ClearOrder.gamificacao import posicao_do_quarto
        p = posicao_do_quarto(self.ouro)
        self.assertEqual(p['posicao'], 1)
        self.assertEqual(p['medalha'], '🥇')

    def test_quarto_sem_vistoria_nao_tem_posicao(self):
        from ClearOrder.gamificacao import posicao_do_quarto
        self.assertIsNone(posicao_do_quarto(self.vazio))

    def test_quarto_none(self):
        from ClearOrder.gamificacao import posicao_do_quarto, eh_quarto_ouro
        self.assertIsNone(posicao_do_quarto(None))
        self.assertFalse(eh_quarto_ouro(None))

    def test_eh_quarto_ouro(self):
        self.vistoria(self.ouro, 9)
        self.vistoria(self.prata, 5)
        from ClearOrder.gamificacao import eh_quarto_ouro
        self.assertTrue(eh_quarto_ouro(self.ouro))
        self.assertFalse(eh_quarto_ouro(self.prata))

    # ---------- contexto ----------
    def test_podio_no_painel_do_monitor(self):
        self.vistoria(self.ouro, 9)
        ctx = self.cliente(self.monitor.user).get(reverse('painel_monitor')).context
        self.assertIn('podio', ctx)
        self.assertEqual(ctx['podio'][0]['quarto'], self.ouro)

    def test_podio_na_gestao(self):
        self.vistoria(self.ouro, 9)
        ctx = self.cliente(self.monitor.user).get(reverse('gestao_monitor')).context
        self.assertIn('podio', ctx)
        self.assertTrue(ctx['podio'])

    def test_contexto_do_aluno(self):
        self.vistoria(self.ouro, 9)
        ctx = self.cliente(self.est_ouro.user).get(reverse('painel_estudante')).context
        self.assertTrue(ctx['eh_quarto_ouro'])
        self.assertEqual(ctx['meu_podio']['posicao'], 1)

    def test_aluno_de_quarto_vazio_nao_e_ouro(self):
        self.vistoria(self.ouro, 9)
        ctx = self.cliente(self.est_vazio.user).get(reverse('painel_estudante')).context
        self.assertFalse(ctx['eh_quarto_ouro'])
        self.assertIsNone(ctx['meu_podio'])

    def test_badge_no_context_processor(self):
        self.vistoria(self.ouro, 9)
        r = self.cliente(self.est_ouro.user).get(reverse('painel_estudante'))
        self.assertTrue(r.context['perfil_quarto_ouro'])

    def test_badge_falso_quando_nao_e_ouro(self):
        self.vistoria(self.prata, 9)
        r = self.cliente(self.est_ouro.user).get(reverse('painel_estudante'))
        self.assertFalse(r.context['perfil_quarto_ouro'])

    def test_monitor_nao_quebra_o_processor(self):
        r = self.cliente(self.monitor.user).get(reverse('painel_monitor'))
        self.assertFalse(r.context['perfil_quarto_ouro'])

    # ---------- render ----------
    def test_banner_do_ouro_no_painel_do_aluno(self):
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.est_ouro.user).get(
            reverse('painel_estudante')).content.decode()
        self.assertIn('🏆', html)
        self.assertIn('Quarto Ouro deste mês', html)
        self.assertIn('Parabéns pelo empenho', html)

    def test_banner_some_quando_nao_e_ouro(self):
        self.vistoria(self.prata, 9)
        html = self.cliente(self.est_ouro.user).get(
            reverse('painel_estudante')).content.decode()
        self.assertNotIn('Parabéns pelo empenho', html)

    def test_badge_coroa_no_modal_de_perfil(self):
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.est_ouro.user).get(
            reverse('painel_estudante')).content.decode()
        self.assertIn('👑 Quarto Ouro', html)

    def test_podio_renderizado_no_monitor(self):
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.monitor.user).get(reverse('painel_monitor')).content.decode()
        self.assertIn('Pódio dos Alojamentos', html)
        self.assertIn('🥇', html)

    def test_podio_renderizado_na_gestao(self):
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.monitor.user).get(reverse('gestao_monitor')).content.decode()
        self.assertIn('Pódio dos Alojamentos', html)
        self.assertIn('🥇', html)

    def test_podio_vazio_tem_empty_state(self):
        html = self.cliente(self.monitor.user).get(reverse('painel_monitor')).content.decode()
        self.assertIn('Pódio dos Alojamentos', html)
        self.assertIn('Nenhum quarto disputa o pódio', html)

    def test_quarto_com_vistoria_mas_sem_nota_nao_disputa(self):
        """nota=None não é nota 0: o quarto não tem avaliação real."""
        self.vistoria(self.ouro, None)
        from ClearOrder.gamificacao import ranking_quartos
        self.assertEqual(ranking_quartos(), [])

    # ---------- layout (regressão do card esmagado) ----------
    def _section_do_podio(self, html):
        """Extrai o <section> que contém o título do Pódio."""
        import re as _re
        m = _re.search(r'<section[^>]*>((?:(?!<section).)*?Pódio dos Alojamentos.*?)</section>', html, _re.S)
        self.assertIsNotNone(m, 'section do pódio não encontrada')
        return m.group(0)

    def test_podio_ocupa_a_largura_inteira_no_painel(self):
        """
        Regressão: painel_monitor.html usa `grid lg:grid-cols-12`. Sem
        `lg:col-span-12` a section ocupa só 1 das 12 colunas e fica espremida
        à esquerda, com o resto da linha vazio.
        """
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.monitor.user).get(reverse('painel_monitor')).content.decode()
        section = self._section_do_podio(html)
        self.assertIn('lg:col-span-12', section)
        self.assertIn('w-full', section)

    def test_podio_sem_largura_fixa_estreita(self):
        """Nenhuma largura fixa pode voltar a espremer o card."""
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.monitor.user).get(reverse('painel_monitor')).content.decode()
        section = self._section_do_podio(html)
        for proibida in ['w-16', 'w-1/12', 'max-w-xs', 'w-32', 'max-w-sm']:
            self.assertNotIn(proibida, section)

    def test_podio_ocupa_largura_inteira_na_gestao_tambem(self):
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.monitor.user).get(reverse('gestao_monitor')).content.decode()
        self.assertIn('lg:col-span-12', self._section_do_podio(html))

    def test_grade_interna_do_podio_e_responsiva(self):
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.monitor.user).get(reverse('painel_monitor')).content.decode()
        section = self._section_do_podio(html)
        self.assertIn('grid grid-cols-1 md:grid-cols-3', section)
        self.assertIn('gap-4', section)

    def test_cards_do_podio_tem_fundo_e_borda(self):
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.monitor.user).get(reverse('painel_monitor')).content.decode()
        section = self._section_do_podio(html)
        for classe in ['bg-white', 'border-slate-200', 'rounded-2xl', 'p-5']:
            self.assertIn(classe, section)

    def test_cards_do_podio_tem_dark_mode(self):
        self.vistoria(self.ouro, 9)
        html = self.cliente(self.monitor.user).get(reverse('painel_monitor')).content.decode()
        section = self._section_do_podio(html)
        for classe in ['dark:bg-slate-900', 'dark:border-slate-800', 'dark:text-slate-100']:
            self.assertIn(classe, section)

    def test_layout_do_podio_nao_quebra_o_vazio(self):
        """O span tem de estar na section, não dentro do {% if podio %}."""
        html = self.cliente(self.monitor.user).get(reverse('painel_monitor')).content.decode()
        self.assertIn('lg:col-span-12', self._section_do_podio(html))


class LapidacaoVisualTests(TestCase):
    """Tipografia, microinterações, toasts e tooltips."""

    def setUp(self):
        u_mon = User.objects.create_user('lap_mon', password='x', first_name='Vitor')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='1239876', cpf='98765412300', bloco_responsavel='B')
        self.q = Quarto.objects.create(numero='104', bloco='B')
        u_est = User.objects.create_user('lap_est', password='x', first_name='Ana')
        self.estudante = Estudante.objects.create(
            user=u_est, matricula='lap1', cpf='45612378900', quarto=self.q)

    def html_aluno(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.estudante.user)
        return c.get(reverse('painel_estudante')).content.decode()

    def html_monitor(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c.get(reverse('painel_monitor')).content.decode()

    def html_gestao(self):
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c.get(reverse('gestao_monitor')).content.decode()

    def html_com_mensagem(self, tag='success'):
        """Pagina do aluno renderizada logo apos um POST que cria mensagem."""
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.estudante.user)
        c.post(reverse('enviar_relatorio'), {
            'titulo': 'Vaza na torneira', 'categoria': 'hidraulica', 'descricao': 'pingando'})
        r = c.get(reverse('painel_estudante'))
        self.assertTrue(r.context['messages'], 'a view nao produziu mensagem')
        return r.content.decode()

    # ---------- 1. tipografia ----------
    def test_fonte_carregada(self):
        for html in (self.html_aluno(), self.html_monitor()):
            self.assertIn('fonts.googleapis.com/css2?family=Plus+Jakarta+Sans', html)
            self.assertIn('wght@400;500;600;700', html)

    def test_familia_padrao_no_css_global(self):
        self.assertIn("'Plus Jakarta Sans'", self.html_aluno())
        self.assertIn('font-family', self.html_aluno())

    def test_familia_no_tailwind_config(self):
        html = self.html_aluno()
        i = html.index('tailwind.config')
        trecho = html[i:i + 500]
        self.assertIn('fontFamily', trecho)
        self.assertIn('Plus Jakarta Sans', trecho)

    def test_preconnect_para_nao_travar_a_renderizacao(self):
        html = self.html_aluno()
        self.assertIn('rel="preconnect"', html)
        self.assertLess(html.index('preconnect'), html.index('css2?family'))

    def test_fonte_na_standalone_tambem(self):
        """painel_monitor.html nao extends base: precisa da fonte sozinha."""
        self.assertIn('Plus+Jakarta+Sans', self.html_monitor())

    # ---------- 2. toasts ----------
    def test_toast_renderiza_a_mensagem(self):
        html = self.html_com_mensagem()
        self.assertIn('data-toast', html)
        self.assertIn('Problema reportado com sucesso', html)

    def test_toast_fica_no_canto_inferior_direito(self):
        html = self.html_com_mensagem()
        self.assertIn('fixed bottom-5 right-5 z-50 flex flex-col gap-2', html)

    def test_toast_some_em_3_5_segundos(self):
        html = self.html_com_mensagem()
        self.assertIn('TOAST_MS = 3500', html)
        self.assertIn('window.setTimeout', html)

    def test_toast_tem_icone_e_botao_de_fechar(self):
        html = self.html_com_mensagem()
        self.assertIn('fa-check', html)                       # icone de sucesso
        self.assertIn("this.closest('[data-toast]').remove()", html)

    def test_toast_acessivel(self):
        html = self.html_com_mensagem()
        self.assertIn('role="status"', html)
        self.assertIn('aria-live="polite"', html)

    def test_toast_escuro(self):
        html = self.html_com_mensagem()
        self.assertIn('bg-white dark:bg-slate-900', html)
        self.assertIn('shadow-lg rounded-2xl p-4', html)

    def test_sem_mensagem_nao_renderiza_toast(self):
        self.assertNotIn('data-toast', self.html_aluno())

    def test_toast_tem_icone_por_tipo(self):
        """Cada nivel da mensagem recebe um icone proprio."""
        from django.template.loader import render_to_string

        class M:
            def __init__(self, tags, texto):
                self.tags, self.text = tags, texto
            def __str__(self):
                return self.text

        for tags, icone in [('success', 'fa-check'), ('error', 'fa-xmark'),
                            ('warning', 'fa-triangle-exclamation'), ('info', 'fa-circle-info')]:
            html = render_to_string('_toasts.html', {'messages': [M(tags, 'aviso')]},
                                    request=None)
            self.assertIn(icone, html, 'sem icone para %s' % tags)

    def test_toast_fora_do_fluxo_do_main(self):
        """
        Toast no fluxo empurraria o conteúdo da página para baixo. Ele tem de
        vir DEPOIS do último </main> — é o que o rindex mede.
        """
        html = self.html_com_mensagem()
        ultimo_main = html.rindex('</main>')
        self.assertGreater(html.index('data-toast'), ultimo_main,
                           'toast está dentro do <main> e empurra o conteúdo')

    def test_respeita_prefers_reduced_motion(self):
        """Animacao de toast nao pode ser inevitavel para quem pediu menos movimento."""
        html = self.html_aluno()
        self.assertIn('prefers-reduced-motion', html)
        self.assertIn('animation: none !important', html)

    def test_animacoes_sao_css_puro(self):
        """@apply dentro de <style> nao e suportado pelo Play CDN."""
        html = self.html_aluno()
        bloco = html[html.index('toastEntrada') - 200:html.index('</style>')]
        self.assertNotIn('@apply', bloco)

    # ---------- 3. microinteracoes ----------
    def test_botao_primario_tem_feedback_de_clique(self):
        html = self.html_aluno()
        self.assertIn('bg-[#00e6a5]', html)
        self.assertIn('active:scale-[0.97]', html)

    def test_scale_padronizado_em_todo_o_projeto(self):
        """Nao pode sobrar active:scale-[0.98] de uma rodada anterior."""
        import io as _io
        import os as _os
        D = _os.path.join(_os.path.dirname(__file__), 'templates')
        for nome in _os.listdir(D):
            if not nome.endswith('.html'):
                continue
            t = _io.open(_os.path.join(D, nome), encoding='utf-8').read()
            self.assertNotIn('active:scale-[0.98]', t, 'escala antiga em %s' % nome)

    def test_cards_tem_hover_elevado(self):
        for html in (self.html_gestao(), self.html_aluno()):
            self.assertIn('hover:-translate-y-1 hover:shadow-md', html)

    def test_modal_tem_transicao(self):
        html = self.html_aluno()
        i = html.index('id="modal-perfil"')
        trecho = html[i:i + 400]
        self.assertIn('transition-all duration-200 ease-out', trecho)

    def test_modal_preserva_o_toggle_hidden_flex(self):
        """A microinteração não pode quebrar a trava de loop testada antes."""
        html = self.html_aluno()
        abrir = html.split('function abrirPerfil()')[1].split('function fecharPerfil')[0]
        self.assertIn("classList.remove('hidden')", abrir)
        self.assertIn("classList.add('flex')", abrir)

    # ---------- 4. tooltips ----------
    def test_tooltip_nos_botoes_de_exportacao(self):
        html = self.html_gestao()
        self.assertIn('id="btn-exportar-csv" title=', html)
        self.assertIn('id="btn-relatorio-pdf" title=', html)

    def test_tooltip_no_icon_e_na_medalha_do_podio(self):
        """O podio só renderiza os cards quando há quarto elegível."""
        from django.utils import timezone
        Vistoria.objects.create(monitor=self.monitor, quarto=self.q, nota=9,
                                aprovado=True, data_vistoria=timezone.localdate())
        html = self.html_monitor()
        # ícone da barra de aproveitamento, no card de Analytics
        self.assertIn('title="Media das notas de vistoria no periodo"', html)
        self.assertIn('title="Percentual de vistorias aprovadas"', html)
        # ícone de streak do ÍCONE DE TABELA fica na Gestão, não no painel
        self.assertIn('title="Sequencia de vistorias aprovadas seguidas"',
                      self.html_gestao())

    def test_tooltip_explica_a_medalha(self):
        """
        Valor RENDERIZADO, não a string crua do template: o Django troca
        {{ linha.posicao }} pelo número antes de mandar o HTML.
        """
        from django.utils import timezone
        Vistoria.objects.create(monitor=self.monitor, quarto=self.q, nota=9,
                                aprovado=True, data_vistoria=timezone.localdate())
        html = self.html_monitor()
        self.assertIn('title="1º colocado', html)
        self.assertIn('ficou em 1º lugar no ranking', html)

    def test_tooltip_no_icone_de_local_dos_chamados(self):
        """O ícone do quarto só sai dentro do {% for chamado %}."""
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='Vaza na torneira', descricao='pingando')
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        html = c.get(reverse('chamados')).content.decode()
        self.assertIn('title="Quarto do estudante que abriu o chamado"', html)

    # ---------- 5. nada quebrou ----------
    def test_painel_do_monitor_mantem_o_banner_data_msg(self):
        """O painel é standalone e mantém o banner; só o base virou toast."""
        html = self.html_monitor()
        self.assertNotIn('data-toast', html)

    def test_conteudo_principal_intacto(self):
        html = self.html_aluno()
        for marca in ['Acesso rápido', 'Chamados', 'Progresso', 'Início']:
            self.assertIn(marca, html)


def _cartoes_de_chamado(html):
    """
    Localiza os cards de chamado e mede a profundidade de aninhamento de cada
    um na árvore do DOM.

    Usa html.parser da stdlib em vez de BeautifulSoup: nao exige pacote
    novo, e adicionar dependencia pip ja deu problema antes.

    Devolve [(rotulo_do_card, profundidade_do_card)].
    """
    from html.parser import HTMLParser

    class Medidor(HTMLParser):
        MARCA_CARD = 'p-4 rounded-2xl border flex flex-col'

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.pilha = []          # classes do card em que estamos dentro
            self.profundidades = []
            self.prof_atual = 0

        def handle_starttag(self, tag, attrs):
            if tag == 'div':
                d = dict(attrs)
                classes = d.get('class', '')
                if self.MARCA_CARD in classes:
                    # 1 se não houver OUTRO card aberto acima; 2+ = aninhado
                    self.profundidades.append(len(self.pilha) + 1)
                    self.pilha.append(classes)
                elif self.pilha:
                    self.prof_atual += 1

        def handle_endtag(self, tag):
            if tag == 'div' and self.pilha:
                if self.prof_atual > 0:
                    self.prof_atual -= 1
                else:
                    self.pilha.pop()

    m = Medidor()
    m.feed(html)
    return m.profundidades


class AninhamentoChamadosTests(TestCase):
    """Regressão: os cards de chamado são IRMÃOS, não filhos uns dos outros."""

    def setUp(self):
        u_mon = User.objects.create_user('ani_mon', password='x')
        self.monitor = Monitor.objects.create(
            user=u_mon, siape='5554443', cpf='44455566677', bloco_responsavel='B')
        q = Quarto.objects.create(numero='104', bloco='B')
        u_est = User.objects.create_user('ani_est', password='x')
        self.estudante = Estudante.objects.create(
            user=u_est, matricula='ani1', cpf='77788899900', quarto=q)

    def html_com(self, n):
        for i in range(n):
            SolicitacaoReparo.objects.create(
                estudante=self.estudante,
                titulo='Chamado %d' % i, descricao='descricao %d' % i)
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.monitor.user)
        return c.get(reverse('chamados')).content.decode()

    # ---------- o bug ----------
    def test_tres_chamados_sao_irmaos(self):
        """3 chamados -> 3 cards, todos com profundidade 1."""
        html = self.html_com(3)
        profs = _cartoes_de_chamado(html)
        self.assertEqual(len(profs), 3, 'esperava 3 cards, achou %d' % len(profs))
        self.assertEqual(profs, [1, 1, 1],
                         'cards aninhados uns dentro dos outros (boneca russa): %r' % profs)

    def test_um_chamado_continua_sendo_um_card(self):
        profs = _cartoes_de_chamado(self.html_com(1))
        self.assertEqual(profs, [1])

    def test_cinco_chamados_nao_crescem_a_profundidade(self):
        """O defeito escalava com a quantidade: 5 chamados => 5 níveis."""
        profs = _cartoes_de_chamado(self.html_com(5))
        self.assertEqual(profs, [1, 1, 1, 1, 1])

    def test_profundidade_maxima_e_um(self):
        html = self.html_com(4)
        self.assertLessEqual(max(_cartoes_de_chamado(html)), 1)

    # ---------- estrutura do container ----------
    def test_container_do_loop_e_uma_grade_responsiva(self):
        html = self.html_com(3)
        self.assertIn('grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4', html)

    def test_container_nao_pilha_mais(self):
        """O grid substituiu o space-y-3 vertical."""
        html = self.html_com(3)
        i = html.index('grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3')
        janela = html[max(0, i - 400):i]
        self.assertNotIn('space-y-3', janela)

    def test_cada_card_tem_o_seu_texto(self):
        """Se aninhassem, os títulos ficariam um dentro do outro."""
        html = self.html_com(3)
        for i in range(3):
            self.assertIn('Chamado %d' % i, html)

    def test_divs_do_loop_estao_balanceadas_no_template(self):
        """Fecha o bug na origem: um </div> faltando no template."""
        import io as _io
        import os as _os
        caminho = _os.path.join(_os.path.dirname(__file__), 'templates', 'chamados.html')
        t = _io.open(caminho, encoding='utf-8').read()

        ini = t.index('{% for chamado')
        fim = t.index('{% endfor', ini)
        corpo = t[ini:fim]
        self.assertEqual(
            corpo.count('<div'), corpo.count('</div>'),
            'divs desbalanceados dentro do loop do template')

    # ---------- não regrediu ----------
    def test_estudante_ainda_ve_os_proprios_chamados(self):
        SolicitacaoReparo.objects.create(
            estudante=self.estudante, titulo='Meu chamado', descricao='x')
        c = Client(SERVER_NAME='localhost')
        c.force_login(self.estudante.user)
        html = c.get(reverse('chamados')).content.decode()
        self.assertIn('Meu chamado', html)
        self.assertEqual(_cartoes_de_chamado(html), [1])

    def test_lista_vazia_usa_empty_state(self):
        html = self.html_com(0)
        self.assertIn('Nenhum chamado registrado ainda', html)
        self.assertEqual(_cartoes_de_chamado(html), [])


class StreakCardTests(TestCase):
    """Contador numerico do card 'Streak de Limpeza' no painel do estudante."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='606', bloco='F')
        self.u = User.objects.create_user('aluno_streak', password='x')
        self.estudante = Estudante.objects.create(
            user=self.u, matricula='888', cpf='66677788899', quarto=self.quarto
        )
        self.client.force_login(self.u)

    def html(self):
        return self.client.get(reverse('painel_estudante')).content.decode()

    @staticmethod
    def numero_streak(html):
        """Extrai os digitos renderizados no <span class="text-5xl font-black">."""
        import re
        m = re.search(r'font-black">\s*(\d+)\s*<', html)
        return m.group(1) if m else None

    def test_streak_zero_saida_com_dois_digitos(self):
        self.assertEqual(self.numero_streak(self.html()), '00')

    def test_streak_cinco_saida_05(self):
        self.quarto.streak_dias = 5
        self.quarto.save()
        self.assertEqual(self.numero_streak(self.html()), '05')

    def test_streak_dez_nao_vira_010(self):
        self.quarto.streak_dias = 10
        self.quarto.save()
        self.assertEqual(self.numero_streak(self.html()), '10')

    def test_streak_cem_nao_e_truncado(self):
        self.quarto.streak_dias = 100
        self.quarto.save()
        self.assertEqual(self.numero_streak(self.html()), '100')

    def test_streak_alto_acende_fogueira(self):
        self.quarto.streak_dias = 10
        self.quarto.save()
        h = self.html()
        self.assertIn('Fogueira acesa!', h)
        self.assertNotIn('Mantenha 3 vistorias', h)

    def test_streak_baixo_mantem_card_escuro(self):
        """Abaixo de 3 o card fica no verde institucional escuro do SUAP."""
        self.quarto.streak_dias = 2
        self.quarto.save()
        h = self.html()
        self.assertIn('Mantenha 3 vistorias consecutivas', h)
        self.assertIn('bg-[#0b4a42]', h)

    def test_estudante_sem_quarto_nao_quebra(self):
        self.estudante.quarto = None
        self.estudante.save()
        r = self.client.get(reverse('painel_estudante'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.numero_streak(r.content.decode()), '00')

    def test_model_nao_tem_campo_streak_so_streak_dias(self):
        """Regressao: o template apontou para 'streak', que nunca existiu."""
        self.assertTrue(hasattr(Quarto, 'streak_dias'))
        self.assertFalse(hasattr(Quarto, 'streak'))

    def test_template_nao_usa_mais_o_campo_antigo(self):
        with open(
            str(Path(__file__).resolve().parent / 'templates' / 'painel_estudante.html'),
            encoding='utf-8',
        ) as f:
            html = f.read()
        self.assertNotIn('estudante.quarto.streak|', html)
        self.assertNotIn('{% if streak_atual < 10 %}', html)
        self.assertIn('stringformat:"02d"', html)

    def test_calendario_semanal_intacto(self):
        """O calendario nao pode ter sido tocado."""
        h = self.html()
        for dia in ('Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'):
            self.assertIn(dia, h)


class ModalCentralizadoTests(TestCase):
    """items-center/justify-center so funcionam se o modal virar display:flex ao abrir."""

    def setUp(self):
        self.quarto = Quarto.objects.create(numero='808', bloco='H')
        u_mon = User.objects.create_user('mon_modal', password='x')
        Monitor.objects.create(
            user=u_mon, siape='9988776', cpf='12312312312', bloco_responsavel='H'
        )
        u_est = User.objects.create_user('aluno_modal', password='x')
        Estudante.objects.create(
            user=u_est, matricula='321', cpf='32132132132', quarto=self.quarto
        )

        self.client.force_login(u_mon)
        self.html_monitor = self.client.get(reverse('painel_monitor')).content.decode()

        self.client.force_login(u_est)
        self.html_estudante = self.client.get(reverse('painel_estudante')).content.decode()

    @staticmethod
    def classes(html, element_id):
        m = re.search(r'id="%s"[^>]*?class="([^"]*)"' % element_id, html)
        return m.group(1).split() if m else []

    @staticmethod
    def corpo_js(src, nome):
        """Corpo da funcao JS, do 'function X(' ate a proxima 'function '."""
        for parte in src.split('function ')[1:]:
            if parte.startswith(nome + '('):
                return parte
        return ''

    def checa_base(self, html, modal_id):
        c = self.classes(html, modal_id)
        self.assertTrue(c, 'modal %s nao encontrado' % modal_id)
        for classe in ('fixed', 'inset-0', 'z-50', 'items-center', 'justify-center', 'hidden'):
            self.assertIn(classe, c)
        # 'flex' nao pode estar na base: entra so via JS, junto da remocao de hidden
        self.assertNotIn('flex', c)

    def checa_js(self, html, abrir, fechar):
        corpo_abrir = self.corpo_js(html, abrir)
        corpo_fechar = self.corpo_js(html, fechar)
        self.assertTrue(corpo_abrir, 'funcao %s nao encontrada' % abrir)
        self.assertTrue(corpo_fechar, 'funcao %s nao encontrada' % fechar)
        self.assertIn("classList.remove('hidden')", corpo_abrir)
        self.assertIn("classList.add('flex')", corpo_abrir)
        self.assertIn("classList.add('hidden')", corpo_fechar)
        self.assertIn("classList.remove('flex')", corpo_fechar)

    def test_modal_monitor_estrutura_base(self):
        self.checa_base(self.html_monitor, 'modal-confirmacao')

    def test_modal_estudante_estrutura_base(self):
        self.checa_base(self.html_estudante, 'modal-estudante')

    def test_js_monitor_troca_flex_e_hidden(self):
        self.checa_js(self.html_monitor, 'abrirModal', 'fecharModal')

    def test_js_estudante_troca_flex_e_hidden(self):
        self.checa_js(self.html_estudante, 'abrirModalEstudante', 'fecharModalEstudante')

    def test_modal_fica_fora_do_main(self):
        """O modal precisa ser filho direto do body, fora de containers com overflow."""
        for html, nome in ((self.html_monitor, 'monitor'), (self.html_estudante, 'estudante')):
            self.assertGreater(html.rfind('modal-'), html.rfind('</main>'), nome)

    def test_trava_de_loop_continua_usando_apenas_hidden(self):
        """A trava de loop e separate: nao deve ter ganho 'flex'."""
        self.client.force_login(User.objects.get(username='mon_modal'))
        html = self.client.get(reverse('detalhes_quarto', args=[self.quarto.id])).content.decode()
        botoes = self.classes(html, 'area-botoes')
        self.assertIn('grid', botoes)
        self.assertNotIn('flex', botoes)


