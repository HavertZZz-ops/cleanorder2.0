from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User


class Quarto(models.Model):
    # Um quarto é identificado por (numero, bloco): o 104 do Bloco A não é o
    # 104 do Bloco C. Sem esta restrição, o admin (ou qualquer código futuro)
    # pode cadastrar o mesmo quarto duas vezes e a moradia se duplica.
    class Meta:
        unique_together = [('numero', 'bloco')]

    STATUS_PENDENTE = 'Pendente'
    STATUS_APROVADO = 'Aprovado'
    STATUS_REPROVADO = 'Reprovado'
    STATUS_CHOICES = [
        (STATUS_PENDENTE, 'Pendente'),
        (STATUS_APROVADO, 'Aprovado'),
        (STATUS_REPROVADO, 'Reprovado'),
    ]

    MODO_SOLO = 'SOLO'
    MODO_DUPLA = 'DUPLA'
    MODO_LIVRE = 'LIVRE'
    MODOS_DIVISAO = [
        (MODO_SOLO, 'Solo'),
        (MODO_DUPLA, 'Dupla'),
        (MODO_LIVRE, 'Livre'),
    ]

    numero = models.CharField(max_length=10)
    bloco = models.CharField(max_length=10)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDENTE)
    streak_dias = models.IntegerField(default=0)
    pontuacao_geral = models.IntegerField(default=0)
    # O representante e sempre um morador deste quarto. A validacao de
    # pertencimento fica na view de gestao; SET_NULL preserva o quarto quando
    # o morador e removido.
    representante = models.ForeignKey(
        'Estudante',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quarto_representado',
    )
    modo_divisao = models.CharField(
        max_length=10,
        choices=MODOS_DIVISAO,
        default=MODO_SOLO,
    )

    @classmethod
    def obter_ou_criar(cls, numero, bloco):
        """
        Devolve o quarto existente ou cria um. É o ponto único de entrada
        para morar em (numero, bloco): reutiliza o registro em vez de
        cadastrar uma linha nova a cada aluno.
        """
        numero = str(numero or '').strip()
        bloco = str(bloco or '').strip()
        if not numero or not bloco:
            raise ValueError('numero e bloco sao obrigatorios')
        quarto, criado = cls.objects.get_or_create(numero=numero, bloco=bloco)
        return quarto, criado

    @property
    def andar(self):
        """
        Andar a partir do primeiro dígito do número do quarto.

        No IFSULDEMINAS a numeração segue a casa: 104 é 1º andar, 205 é 2º,
        301 é 3º, 412 é 4º. Numeração sem dígito cai em "1º andar".
        """
        numero = str(self.numero or '')
        for caractere in numero:
            if caractere.isdigit():
                return '%dº andar' % int(caractere)
        return '1º andar'

    def __str__(self):
        return f"Quarto {self.numero} - Bloco {self.bloco}"

class Estudante(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    matricula = models.CharField(max_length=20, unique=True)
    cpf = models.CharField(max_length=11, unique=True, null=True, blank=True)
    quarto = models.ForeignKey(Quarto, on_delete=models.SET_NULL, null=True, blank=True)
    # Opcional de proposito: quem ja existia no banco fica com ''.
    foto_perfil = models.ImageField(upload_to='perfis/', null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} ({self.matricula})"

class Monitor(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    siape = models.CharField(max_length=7, unique=True)
    cpf = models.CharField(max_length=11, unique=True, null=True, blank=True)
    bloco_responsavel = models.CharField(max_length=10)

    def __str__(self):
        return f"Monitor: {self.user.username} - Bloco {self.bloco_responsavel}"

class Cronograma(models.Model):
    quarto = models.ForeignKey(Quarto, on_delete=models.CASCADE)
    data_inicio = models.DateField()
    data_fim = models.DateField()
    ativo = models.BooleanField(default=True)

    def __str__(self):
        return f"Cronograma: {self.quarto} ({self.data_inicio} a {self.data_fim})"

class Tarefa(models.Model):
    cronograma = models.ForeignKey(Cronograma, on_delete=models.CASCADE)
    # SOLO/DUPLA preenchem o responsavel principal no momento da geracao.
    # LIVRE nasce com os dois campos vazios: a tarefa fica aberta para que
    # qualquer morador do quarto a assuma ao concluir.
    estudante_responsavel = models.ForeignKey(
        Estudante,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    estudante_responsavel_2 = models.ForeignKey(
        Estudante,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tarefas_secundarias',
    )
    titulo = models.CharField(max_length=100)
    descricao = models.TextField()
    data_prazo = models.DateField()
    status = models.CharField(max_length=50, default='Pendente')

    @property
    def aberta(self):
        """True quando nenhum morador foi designado (modo LIVRE)."""
        return self.estudante_responsavel_id is None

    @property
    def responsaveis(self):
        """Lista com os moradores designados, sem duplicatas."""
        pessoas = []
        for pessoa in (self.estudante_responsavel, self.estudante_responsavel_2):
            if pessoa is not None and pessoa not in pessoas:
                pessoas.append(pessoa)
        return pessoas

    def __str__(self):
        return self.titulo

class Vistoria(models.Model):
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE)
    quarto = models.ForeignKey(Quarto, on_delete=models.CASCADE)
    data_vistoria = models.DateField(default=timezone.now)
    nota = models.IntegerField(null=True, blank=True)
    observacoes = models.TextField(blank=True, null=True)
    aprovado = models.BooleanField()

    def __str__(self):
        return f"Vistoria: {self.quarto} - {'Aprovado' if self.aprovado else 'Reprovado'}"

class Notificacao(models.Model):
    estudante_destinatario = models.ForeignKey(Estudante, on_delete=models.CASCADE)
    titulo = models.CharField(max_length=100)
    mensagem = models.TextField()
    data_envio = models.DateTimeField(default=timezone.now)
    lida = models.BooleanField(default=False)

    def __str__(self):
        return f"Para:{self.estudante_destinatario.user.username} - {self.titulo}"

class SolicitacaoReparo(models.Model):
    estudante = models.ForeignKey(Estudante, on_delete=models.CASCADE)
    titulo = models.CharField(max_length=150, default="Sem título") # Campo novo
    categoria = models.CharField(max_length=50, default="outros")   # Campo novo
    descricao = models.TextField()
    data_abertura = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=50, default='Em Aberto')
    # Opcional de proposito: chamado sem foto continua funcionando igual.
    imagem = models.ImageField(upload_to='chamados/', null=True, blank=True)

    def __str__(self):
        return f"Chamado {self.id} - {self.estudante.quarto}"