# -*- coding: utf-8 -*-
"""
Gerador de QR Code em Python puro (ISO/IEC 18004).

POR QUE EXISTE: a maquina nao alcanca o PyPI (timeout) e a biblioteca JS
(qrcodejs) vinha de CDN - que falha justamente no notebook em hotspot, o
cenario em que a demonstracao acontece. Sem dependencia e sem rede, o QR
sempre funciona.

ESCOPO (deliberadamente restrito ao que o projeto precisa):
  - modo BYTE (URLs sao ASCII)
  - nivel de correcao M (~15% de recuperacao - QR colado na porta suffers
    sujeira e desbotamento)
  - versoes 1 a 10, escolhidas automaticamente pelo tamanho do texto

Tudo que nao e necessario para esse caso foi deixado de fora.
"""
from xml.sax.saxutils import escape

# --------------------------------------------------------------------------
# 1. Campo de Galois GF(256), polinomio gerador 0x11D
# --------------------------------------------------------------------------
_EXP = [0] * 512
_LOG = [0] * 256


def _inicializa_gf():
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_inicializa_gf()


def _gf_mult(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _gf_pow(a, n):
    return _EXP[(_LOG[a] * n) % 255] if a else 0


def _gerador_polinomio(degree):
    """Polinomio gerador de Reed-Solomon de grau `degree`."""
    pol = [1]
    for i in range(degree):
        pol = _polinomio_mult(pol, [1, _EXP[i]])
    return pol


def _polinomio_mult(a, b):
    """Multiplica polinomios (coeficiente de maior grau primeiro)."""
    resultado = [0] * (len(a) + len(b) - 1)
    for i, ai in enumerate(a):
        if ai == 0:
            continue
        for j, bj in enumerate(b):
            resultado[i + j] ^= _gf_mult(ai, bj)
    return resultado


def _rs_codificar(dados, ec_count):
    """Palavra de codigo de Reed-Solomon (remainder da divisao polinomial)."""
    gerador = _gerador_polinomio(ec_count)
    resto = [0] * ec_count
    for byte in dados:
        fator = byte ^ resto[0]
        resto = resto[1:] + [0]
        for i, coeficiente in enumerate(gerador[1:]):
            resto[i] ^= _gf_mult(coeficiente, fator)
    return resto


# --------------------------------------------------------------------------
# 2. Tabelas da especificacao
#    versao -> (total, ec_por_bloco, blocos_g1, dados_g1, blocos_g2, dados_g2)
# --------------------------------------------------------------------------
_TABELA_M = {
    1:  (26,  10, 1, 16, 0, 0),
    2:  (44,  16, 1, 28, 0, 0),
    3:  (70,  26, 1, 44, 0, 0),
    4:  (100, 18, 2, 32, 0, 0),
    5:  (134, 24, 2, 43, 0, 0),
    6:  (172, 16, 4, 27, 0, 0),
    7:  (196, 18, 4, 31, 0, 0),
    8:  (242, 22, 2, 38, 2, 39),
    9:  (292, 22, 3, 36, 2, 37),
    10: (346, 26, 4, 43, 1, 44),
}

# Posicoes dos centros dos padroes de alinhamento por versao.
_alinhamento = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

# Bits do indicador de formato por nivel de correcao.
# M = nivel 0b00
_INDICADOR_FORMATO_M = 0b00
_MASCARAS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _bits(dado):
    """Bytes -> string de bits (MSB primeiro)."""
    return ''.join(format(b, '08b') for b in dado)


def _capacidade_bytes(versao):
    """Quantos bytes cabem em modo BYTE, nivel M, nesta versao."""
    _, _, g1n, g1d, g2n, g2d = _TABELA_M[versao]
    codewords_dados = g1n * g1d + g2n * g2d
    # 4 bits de modo + 8 bits de tamanho (16 a partir da versao 10)
    cabecalho = 4 + (16 if versao >= 10 else 8)
    return (codewords_dados * 8 - cabecalho) // 8


def _escolher_versao(tamanho_bytes):
    for versao in range(1, 11):
        if _capacidade_bytes(versao) >= tamanho_bytes:
            return versao
    raise ValueError(
        'Texto longo demais (%d bytes) para QR versão 10 em modo BYTE.'
        % tamanho_bytes)


def _montar_bits_dados(texto, versao):
    """Modo BYTE: [0100] + tamanho + dados + terminador + padding."""
    dados = texto.encode('utf-8') if not isinstance(texto, bytes) else texto
    _, _, g1n, g1d, g2n, g2d = _TABELA_M[versao]
    capacidade_bits = (g1n * g1d + g2n * g2d) * 8

    bits = '0100'
    bits += format(len(dados), '08b' if versao < 10 else '016b')
    bits += _bits(dados)

    # terminador (no maximo 4 bits zeros)
    bits += '0' * min(4, capacidade_bits - len(bits))
    # preenche ate o limite do byte
    while len(bits) % 8 != 0:
        bits += '0'
    # bytes de padding alternados 0xEC / 0x11
    alvo = capacidade_bits // 8
    preenchimento = [0xEC, 0x11]
    i = 0
    while len(bits) // 8 < alvo:
        bits += _bits([preenchimento[i % 2]])
        i += 1
    return bits


def _intercalar(blocos_dados, blocos_ec, versao):
    """
    Intercalacao da especificacao: alterna os codewords entre os blocos para
    que um borrão nao destrua um bloco inteiro.
    """
    _, ec, g1n, g1d, g2n, g2d = _TABELA_M[versao]
    n_blocos = g1n + g2n
    tamanhos = [g1d] * g1n + [g2d] * g2n

    saida = []
    for posicao in range(max(tamanhos)):
        for b in range(n_blocos):
            if posicao < len(blocos_dados[b]):
                saida.append(blocos_dados[b][posicao])
    for posicao in range(ec):
        for b in range(n_blocos):
            saida.append(blocos_ec[b][posicao])
    return saida


# --------------------------------------------------------------------------
# 3. Construcao da matriz
# --------------------------------------------------------------------------
def _matriz(version):
    n = 17 + 4 * version
    return [[None] * n for _ in range(n)]


def _colocar_padroes(m, version):
    n = len(m)

    def bloco_pesquisa(linha, coluna):
        for i in range(-1, 8):
            for j in range(-1, 8):
                r, c = linha + i, coluna + j
                if 0 <= r < n and 0 <= c < n:
                    dentro = (0 <= i <= 6 and j in (0, 6)) or \
                             (0 <= j <= 6 and i in (0, 6)) or \
                             (2 <= i <= 4 and 2 <= j <= 4)
                    m[r][c] = dentro

    bloco_pesquisa(0, 0)
    bloco_pesquisa(0, n - 7)
    bloco_pesquisa(n - 7, 0)

    # separadores (ja ficam None, garantido pela area -1..7)

    # modulos de tempo
    for i in range(8, n - 8):
        m[6][i] = i % 2 == 0
        m[i][6] = i % 2 == 0

    # padroes de alinhamento
    centros = _alinhamento[version]
    for linha in centros:
        for coluna in centros:
            # nao sobrescrever os tres blocos de busca
            if (linha, coluna) in ((6, 6), (6, centros[-1]), (centros[-1], 6)):
                continue
            for i in range(-2, 3):
                for j in range(-2, 3):
                    borda = max(abs(i), abs(j))
                    m[linha + i][coluna + j] = borda != 1

    # modulo escuro obrigatorio: COLUNA 8, linha n-8 (sempre dark)
    m[n - 8][8] = True

    # reservar areas de formato (preenchidas depois por _colocar_formato)
    for i in range(9):
        if m[8][i] is None:
            m[8][i] = False          # linha 8, colunas 0..8
        if m[i][8] is None:
            m[i][8] = False          # coluna 8, linhas 0..8
    for i in range(8):
        m[8][n - 1 - i] = False      # linha 8, colunas n-1..n-8
        m[n - 1 - i][8] = False      # coluna 8, linhas n-1..n-8

    # reservar areas de versao (versoes >= 7)
    if version >= 7:
        for i in range(6):
            for j in range(3):
                m[n - 11 + j][i] = False
                m[i][n - 11 + j] = False


def _modulo_de_funcao(m, r, c):
    """
    True se o modulo em (r,c) pertence a um padrao de funcao (nao recebe dado).

    A matriz e a UNICA fonte da verdade: depois de _colocar_padroes, tudo que
    nao foi reservado continua None. Reimplementar a geometria aqui criava
    divergencia (p.ex.: alinhamento nao desenhado por sobrepor o finder, mas
    ainda marcado como funcao) e o codigo ficava sem slots.
    """
    return m[r][c] is not None


def _colocar_dados(m, version, bits):
    n = len(m)
    coluna = n - 1
    subir = True
    indice = 0
    total = len(bits)

    while coluna > 0:
        if coluna == 6:
            coluna -= 1
        for i in range(n):
            linha = (n - 1 - i) if subir else i
            for c in (coluna, coluna - 1):
                if _modulo_de_funcao(m, linha, c):
                    continue
                if indice < total:
                    m[linha][c] = bits[indice] == '1'
                    indice += 1
                # o que sobrar ja vem False (modulo claro)
        coluna -= 2
        subir = not subir

    if indice < total:
        raise ValueError('capacidade do QR insuficiente para o payload')


def _bits_bch(valor, gerador, tamanho):
    resultado = valor << (tamanho - 1)
    while resultado.bit_length() >= tamanho:
        resultado ^= gerador << (resultado.bit_length() - tamanho)
    return format(resultado, '0%db' % (tamanho - 1))


def _colocar_formato(m, version, mascara):
    """
    Indicador de formato nas DUAS copias da especacao.

    Convenção daqui: m[linha][coluna]. A copia vertical fica na COLUNA 8
    (linhas 0-5, 7, 8 e n-7..n-1); a horizontal na LINHA 8
    (colunas 0-5, 7, 8 e n-8..n-1).
    """
    n = len(m)
    dados = (_INDICADOR_FORMATO_M << 3) | mascara
    bch = int(_bits_bch(dados, 0b10100110111, 11), 2)
    completo = ((dados << 10) | bch) ^ 0b101010000010010

    def bit(i):
        return bool((completo >> i) & 1)

    # --- copia 1 (esquerda) ---
    for i in range(6):
        m[i][8] = bit(i)          # coluna 8, linhas 0..5
    m[7][8] = bit(6)
    m[8][8] = bit(7)
    m[8][7] = bit(8)              # coluna 7, linha 8
    for i in range(9, 15):
        m[8][14 - i] = bit(i)     # linha 8, colunas 5..0

    # --- copia 2 (direita/base) ---
    for i in range(8):
        m[8][n - 1 - i] = bit(i)  # linha 8, colunas n-1..n-8
    for i in range(8, 15):
        m[n - 15 + i][8] = bit(i)  # coluna 8, linhas n-7..n-1

    m[n - 8][8] = True            # modulo escuro: COLUNA 8, linha n-8


def _colocar_versao(m, version):
    if version < 7:
        return
    n = len(m)
    bch = int(_bits_bch(version, 0b1111100100101, 13), 2)
    completo = (version << 12) | bch
    for i in range(18):
        bit = bool((completo >> i) & 1)
        m[n - 11 + i % 3][i // 3] = bit
        m[i // 3][n - 11 + i % 3] = bit


def _penalidade(m):
    """Regra de avaliacao de mascara da especificacao (menor = melhor)."""
    n = len(m)
    p = 0

    # Regra 1: sequencias de 5+ modulos iguais
    for linha in list(m) + [list(col) for col in zip(*m)]:
        seq = 1
        for i in range(1, n):
            if linha[i] == linha[i - 1]:
                seq += 1
            else:
                if seq >= 5:
                    p += 3 + (seq - 5)
                seq = 1
        if seq >= 5:
            p += 3 + (seq - 5)

    # Regra 2: blocos 2x2 da mesma cor
    for r in range(n - 1):
        for c in range(n - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                p += 3

    # Regra 3: padrao 1:1:3:1:1 com 4 claros de um lado
    padrao_a = [True, False, True, True, True, False, True, False, False, False, False]
    padrao_b = list(reversed(padrao_a))
    for linha in list(m) + [list(col) for col in zip(*m)]:
        for i in range(n - 10):
            if linha[i:i + 11] == padrao_a or linha[i:i + 11] == padrao_b:
                p += 40

    # Regra 4: desvio da proporcao 50/50
    escuros = sum(1 for linha in m for v in linha if v)
    total = n * n
    k = abs(100 * escuros // total - 50) // 5
    p += k * 10
    return p


def gerar_matriz(texto):
    """Texto -> matriz booleana pronta para virar SVG."""
    dados = texto.encode('utf-8') if not isinstance(texto, bytes) else texto
    version = _escolher_versao(len(dados))

    bits = _montar_bits_dados(texto, version)
    codewords = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]

    _, ec, g1n, g1d, g2n, g2d = _TABELA_M[version]
    n_blocos = g1n + g2n
    tamanhos = [g1d] * g1n + [g2d] * g2n

    # separa os codewords entre os blocos (o ultimo bloco pode ser menor)
    blocos = []
    pos = 0
    for tamanho in tamanhos:
        blocos.append(codewords[pos:pos + tamanho])
        pos += tamanho

    blocos_ec = [_rs_codificar(b, ec) for b in blocos]
    intercalado = _intercalar(blocos, blocos_ec, version)

    bits_final = _bits([c & 0xFF for c in intercalado])

    base = _matriz(version)
    _colocar_padroes(base, version)
    _colocar_versao(base, version)

    # escolhe a mascara com menor penalidade
    n = len(base)
    melhor = None
    for indice_mascara, mascara in enumerate(_MASCARAS):
        teste = [linha[:] for linha in base]

        # 1) coloca os dados (preenche os slots None)
        _colocar_dados(teste, version, bits_final)

        # 2) SÓ agora aplica a mascara, e apenas onde base era None.
        #    Aplicar antes faria _modulo_de_funcao ver os slots ja
        #    preenchidos e nao encontraria onde escrever.
        for r in range(n):
            for c in range(n):
                if base[r][c] is None:
                    teste[r][c] = bool(teste[r][c]) ^ mascara(r, c)

        _colocar_formato(teste, version, indice_mascara)

        p = _penalidade(teste)
        if melhor is None or p < melhor[0]:
            melhor = (p, indice_mascara, teste)

    _, mascara_escolhida, matriz = melhor
    # garante que nenhum modulo ficou None
    for r in range(len(matriz)):
        for c in range(len(matriz)):
            if matriz[r][c] is None:
                matriz[r][c] = False
    return matriz, version, mascara_escolhida


def para_svg(texto, cor_escuro='#0b4a42', cor_claro='#ffffff', margem=4):
    """Matriz -> SVG compacto (um unico <path>)."""
    matriz, _, _ = gerar_matriz(texto)
    n = len(matriz)
    lado = n + margem * 2

    partes = []
    for r, linha in enumerate(matriz):
        c = 0
        while c < n:
            if linha[c]:
                inicio = c
                while c < n and linha[c]:
                    c += 1
                comprimento = c - inicio
                partes.append('M%d %dh%dv1h-%dz'
                              % (inicio + margem, r + margem, comprimento, comprimento))
            else:
                c += 1

    caminho = ''.join(partes)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
        'shape-rendering="crispEdges" role="img" aria-label="QR Code">'
        '<rect width="%d" height="%d" fill="%s"/>'
        '<path d="%s" fill="%s"/>'
        '</svg>' % (lado, lado, lado, lado, cor_claro, caminho, cor_escuro)
    )
