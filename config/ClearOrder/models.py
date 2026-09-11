from django.db import models

from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User


class Quarto(models.Model):
    numero = models.CharField(max_length=10)
    bloco = models.CharField(max_length=10)
    streak_dias = models.IntegerField(default=0)
    pontuacao_geral = models.IntegerField(default=0)

    def __str__(self):
        return f"Quarto {self.numero} - Bloco {self.bloco}"

class Estudante(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    matricula = models.CharField(max_length=20, unique=True)
    cpf = models.CharField(max_length=11, unique=True, null=True, blank=True)
    quarto = models.ForeignKey(Quarto, on_delete=models.SET_NULL, null=True, blank=True)

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
    estudante_responsavel = models.ForeignKey(Estudante, on_delete=models.CASCADE)
    titulo = models.CharField(max_length=100)
    descricao = models.TextField()
    data_prazo = models.DateField()
    status = models.CharField(max_length=50, default='Pendente')

    def __str__(self):
        return self.titulo

class Vistoria(models.Model):
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE)
    quarto = models.ForeignKey(Quarto, on_delete=models.CASCADE)
    data_vistoria = models.DateField(default=timezone.now)
    nota = models.DecimalField(max_digits=5, decimal_places=2)
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
    descricao = models.TextField()
    data_abertura = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=50, default='Em Aberto')

    def __str__(self):
        return f"Chamado {self.id} - {self.estudante.quarto}"
