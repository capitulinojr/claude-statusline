# statusline.py - uma status line de duas linhas pro Claude Code

Read this in [English](README.md).

```
Opus 5 (1M context)  medium  |  Tuning the status line  ·  11.0%  2.9M
------- 4.0%  ·  4h 47m  |  48.5%  ·  21.0%  |  32.0%  37.1%  4d 14h -------
```

A linha de cima é esta sessão. A de baixo é a cota da tua conta. Tem uma terceira linha, com um espaço só, pra barra não encostar no prompt logo abaixo.

Nenhuma das duas tem rótulo. Posição e cor já dizem o que é. Tu lê de relance: em que modelo eu estou, quanto de contexto sobra, quanto de cota eu queimei.

Um arquivo, stdlib, zero dependências. Ele lê JSON do stdin e imprime duas linhas. É essa a arquitetura inteira.

---

## Instalação

**Requisitos:** Python 3.8+ no PATH. Mais nada.

1. Salva o `statusline.py` onde tu quiser (ex.: `~/.claude/statusline.py`).

2. No `~/.claude/settings.json`, põe:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python -S -E \"C:\\full\\path\\statusline.py\"",
    "padding": 0,
    "refreshInterval": 15
  }
}
```

No macOS/Linux, usa `python3 -S -E \"/path/statusline.py\"`. Os flags `-S -E` pulam o `site-packages` e as variáveis de ambiente `PYTHON*`: inicia mais rápido, e um `PYTHONPATH` sujo não quebra ele.

3. Confere: `python statusline.py --selftest` tem que imprimir `OK - selftest (0 failure(s))`.

---

## O que é cada campo

**Linha 1 - a sessão**

| Campo | O que é |
| --- | --- |
| `Opus 5 (1M context)` | modelo, colorido por tier (Fable laranja e **negrito**, Opus ciano, Sonnet amarelo, Haiku azul) |
| `medium` | nível de reasoning effort (`low` … `max`) |
| `Tuning the status line` | nome da sessão, em itálico - o que tu pôs no `/rename`, ou o título que o Claude Code gerou. O script tira os caracteres de controle e trunca o texto antes de ele chegar no teu terminal |
| `11.0%` | **contexto** - % da janela inteira já ocupada. Passou de 75%, fica vermelho e o aviso `/compact` aparece do lado |
| `2.9M` | tokens acumulados nesta sessão (input + output + cache) |

**Linha 2 - as cotas**

| Campo | O que é |
| --- | --- |
| `4.0%` | cota da janela de **5 horas** |
| `4h 47m` | tempo até o reset das 5 horas |
| `48.5%` | **teto diário do modelo caro** (ver abaixo) |
| `21.0%` | **teto diário da cota total** - mesmo racionamento, todos os modelos |
| `32.0%` | cota da janela **semanal** |
| `37.1%` | quanto do teto do Fable (metade da cota semanal) já foi |
| `4d 14h` | tempo até o reset semanal |

O bloco do meio tem **dois** números, nesta ordem: teto diário do Fable · teto diário total. Campo sem dado por trás some da barra, então o número de campos varia. A posição é sempre relativa aos separadores `|`, nunca fixa.

### A cor não é o valor, é o ritmo

80% da semana no dia 7 sai amarelo. Os mesmos 80% no dia 2 saem vermelho.

Não é bug. No dia 7 a projeção dá ~86: o ciclo está acabando junto contigo. No dia 2 a projeção passa de 250, e a cota acaba na quarta. Uma escala por valor pintaria os dois iguais, sendo que são situações **opostas**.

É como o combustível do carro. Meio tanque a 10 km de casa não quer dizer a mesma coisa que meio tanque a 400 km.

Por isso toda porcentagem que tem prazo (5 horas, dia, semana) é pintada pelo consumo **projetado** até o reset, e não pelo valor bruto:

```
projeção = consumo ÷ fatia do prazo já decorrida   (100 = cai exatamente no teto)
```

Termômetro (`SCALE_PACE`): cinza, azul, verde, amarelo, laranja, todos apagados, e **vermelho vivo só quando a projeção passa de 135** - ou seja, quando o ritmo atual estoura o teto com folga. Nada mais grita.

Duas exceções, de propósito:

- **Contexto** não tem prazo (não reseta sozinho), então fica no termômetro por valor: vermelho de 75% pra cima, com o aviso `/compact` do lado.
- **Cota real acima de `PACE_HARD` (95%)** volta pro vermelho, tenha o ritmo que tiver. Ali o bloqueio chega antes do reset, e isso é acionável até na véspera.

Sem um `resets_at` usável no payload não existe prazo pra medir contra. O campo cai pra escala por valor, e os campos que dependem de "quantos dias faltam" somem da barra em vez de virarem chute.

---

## O teto do modelo caro (os dois números do Fable)

O **teto** aqui é oficial. O que o script estima é quanto dele tu já gastou.

**O problema:** o Claude Code te entrega a cota agregada (`seven_day.used_percentage`) e nunca diz quanto daquilo foi o modelo caro. Se tu orquestra com Fable e executa com Sonnet/Haiku, o número agregado não te conta se tu está queimando cota no tier errado.

**A estimativa:** o script varre os transcripts (`~/.claude/projects/**/*.jsonl`) da semana corrente. De cada entrada ele tira o **custo ponderado**, pelo preço do modelo daquela entrada: input, output, cache-write e cache-read pesam diferente. Aí projeta a fatia sobre o agregado oficial:

```
fable_share  = fable_cost / total_cost              (sobre os 7 dias)
pontos_gastos_pelo_fable = fable_share × seven_day.used_percentage
% do teto = pontos ÷ 50 × 100                       (FABLE_CAP_SHARE = 50%)
```

`FABLE_CAP_SHARE = 0.50` é o **limite declarado pela Anthropic** (conferido em 2026-07-29), não é chute: nos planos Max e Team Premium, o Fable 5 pode consumir *até metade* da tua cota semanal. Passou disso, ou tu segue no Fable com usage credits, ou troca de modelo pra ficar dentro do que sobrou. Daí a régua: 100% neste campo é o ponto em que o Fable deixa de estar incluído na assinatura. Termo de plano muda - confere a página do teu plano antes de confiar nessa constante.

(Se o teu plano tem outros termos - Enterprise por assento, por exemplo, onde o Fable é só por crédito - ajusta a constante no topo do arquivo.)

### O teto diário se mexe

Queima dois dias de uma vez numa terça e o teto de quarta já nasce menor, então a porcentagem sobe mais rápido. Estourar o orçamento de hoje não mexe no número de hoje. **Estreita** os dias seguintes.

```
teto_de_hoje = (50 pontos − o que o Fable gastou ANTES de hoje) ÷ dias até o reset (contando hoje)
```

Foi a parte chata, e é o que faz o número servir pra alguma coisa. Três detalhes:

- **O teto é fixado à meia-noite**, não recalculado a cada gasto. Se ele encolhesse junto com o consumo, a régua nunca chegaria a 100%.
- **O dia do reset conta inteiro.** Na véspera, o dia herda todo o saldo que restou. O que sobrou é gastável até a hora do reset, e racionar isso por fração não faria sentido.
- **Não tem teto em 100%.** Passar da fatia do dia é legítimo e tem que aparecer. Sem saldo nenhum ao acordar, o dia nasce em 100%.

A cor desses dois campos também vem do ritmo, medida contra o **fim do dia**. E no dia do reset semanal o "dia" é recortado pela semana, não pela meia-noite: se o reset é ao meio-dia, o dia-cota da manhã termina às 12:00 e o da tarde começa às 12:00. Sem esse recorte, 80% do teto às 11:00 projetaria 175% (vermelho) em vez de ~87%. O recorte casa com a medição, que também só enxerga gasto desde o início da semana.

**Por que estimar em vez de ler o número oficial?** Porque ele não existe no payload. A [doc da status line](https://code.claude.com/docs/en/statusline) expõe só `rate_limits.five_hour` e `rate_limits.seven_day`, sem quebra por modelo (conferido em 2026-07-26; se aparecer um dia, troca a estimativa por ele). E a ponderação real também não dá pra reconstruir: a Anthropic publica a cota em *horas de modelo*, com faixas largas (Max 5x: 15-35h de Opus por semana), nunca como peso por token.

Então: o **teto** dos dois números é oficial, a posição dentro dele é estimativa. Uma bússola, não contabilidade.

E dá pra dizer de quanto é o erro. O painel do Claude Code (`/usage`) mostra o número oficial do modelo caro, que o payload não entrega. Comparei os dois no mesmo instante, em 2026-07-29: o oficial dizia **71%** e a estimativa dizia **77,9%**. Sete pontos **para cima**.

O erro é conservador, então ele alarma cedo, nunca tarde. E a causa está na frase do parágrafo anterior: a Anthropic raciona por horas de modelo, o script pondera por custo em dólar. São proxies diferentes, e nenhum ajuste de constante conserta isso de verdade - só disfarça numa amostra.

Se quiseres o número exato, olha o `/usage`. A barra é pra tu não precisar olhar.

Dois detalhes do payload que valem pra qualquer status line. O `rate_limits` só aparece pra assinante Pro/Max **depois da primeira resposta da API na sessão**, e cada janela pode faltar sozinha - por isso tudo aqui passa por `safe()`. E o `context_window.used_percentage` é calculado **só com os tokens de input** (`input + cache_creation + cache_read`, sem `output_tokens`); o fallback do script usa a mesma fórmula pra não divergir do número oficial.

**Não usa o modelo caro, ou só quer as cotas oficiais?** Faz `fable_cap_percent()` e `daily_total_percent()` retornarem `None` na primeira linha. Os três campos somem e a varredura dos transcripts para de rodar.

Apagar os campos do `render()` não basta: as duas funções já teriam sido chamadas, e são elas que disparam a varredura. O que custa é a chamada, não o display.

---

## Ajustes

Tudo que tu ia querer mudar mora nas constantes do topo, cada uma com um comentário:

- `MODEL_COLORS`, `EFFORT_COLORS` - cores por modelo e por effort (xterm-256).
- `SCALE_PACE`, `SCALE_CTX`, `SCALE_FABLE` - os termômetros. Cada um é uma tupla de `(limite superior exclusivo, código SGR)` mais uma cor de alerta acima de tudo. O número que entra no `SCALE_PACE` é a **projeção**, não o consumo. Mexer nos limiares dele é mexer em "a partir de quanto fora do ritmo conta como fora do ritmo". `SCALE` é só o fallback pra quando o payload não traz prazo.
- `PACE_FLOOR_WINDOW` (0.15) e `PACE_FLOOR_DAY` (0.35) - piso do denominador da projeção. No começo de uma janela, extrapolar é ruído (2% gastos sobre 1% do tempo projetariam 200%), então antes desse ponto a janela conta como se o piso já tivesse decorrido. O piso do dia é maior porque o dia do calendário começa à meia-noite e o uso humano não.
- `PACE_HARD` (95) - acima disso, cota real volta a ser pintada por valor.
- `RESET_TOLERANCE` (300 s) - folga pra aceitar um `resets_at` recém-vencido, já que o payload demora alguns segundos pra virar depois do reset.
- `SCAN_DEADLINE` (8 s) - orçamento de tempo da varredura dos transcripts. Passou disso, a estimativa da rodada é descartada e vale o último cache, mesmo velho.
- `PRICES` - USD por 1M tokens, casados pelo prefixo do id do modelo. **Confere as tabelas de preço antes de confiar no número** (as do arquivo foram verificadas em julho de 2026); o casamento é por prefixo, então a ordem das entradas importa.
- `FABLE_CAP_SHARE` - a fatia da cota semanal que o Fable pode ocupar (0.50 = o limite oficial no Max/Team Premium).
- `CTX_HINT` - o texto que aparece quando o contexto passa de 75%.
- `QUOTA_DASHES` - o risquinho que abre e fecha a segunda linha.

---

## Limitações

A projeção é linear e o uso humano vem em rajada, então ela lê pessimista de manhã e otimista de madrugada. Os pisos só cortam a explosão numérica do começo da janela; dentro deles a cor não anda com o relógio. Calibrar uma curva por horas ativas exigiria histórico de uso.

`PACE_HARD` é degrau, não gradiente. 94.9% projetando 95.9 sai amarelo, e 95.0% vai pro vermelho na hora. É de propósito: perto do bloqueio, o valor absoluto toma a cor de volta.

O script lê o relógio várias vezes por render (`time.time()`, `day_start()`). Um render que atravessa a meia-noite pode misturar a fatia de um dia com o custo do outro. Uma barra torta por dia, até o refresh seguinte.

O que ele garante: nunca derruba a sessão. Cada pedaço da barra roda dentro de um `safe()`, e o que falha vira string vazia e some sem deixar buraco. Payload vazio ou inválido imprime linha em branco, não stack trace.

---

## Detalhes de implementação que valem saber

- **O payload vem de fora, então é tratado como hostil.** `(x or {}).get(...)` só protege contra `None`. Um `"model": "bad"` passa reto e estoura no meio do render, apagando a barra inteira. Todo acesso aninhado passa por `as_dict`/`as_text`, e todo número por `finite_number`. Ele rejeita `bool` (um `int` em Python) e `NaN`/`Infinity`, que o `json.loads` aceita por padrão. `NaN` foi o pior: ele sobrevive ao clamp e sai pelo lado errado - `min(100.0, nan)` devolve `100.0` - e teria virado um alerta falso de cota cheia.
- **Prazo implausível é tratado como prazo ausente.** Um `resets_at` no passado, zerado, ou a 99 dias não é prazo ruim, é dado inválido. Montar uma janela em cima dele fabrica um número com cara de medido: o teto diário nascia num vermelho 100% só porque o payload não trazia reset e o código chutava "faltam 7 dias".
- **Com os transcripts é igual.** São arquivos de outro programa, não um contrato: contador de token chega como string, negativo ou `NaN` com a mesma facilidade, então cada um é limpo antes de entrar numa soma. Um `NaN` sozinho envenenaria todas as somas seguintes, em silêncio.
- **Nada escrito pro terminal é confiável.** O nome da sessão é escrito por outro alguém: tu, ou o modelo resumindo a conversa. Solto num terminal, um `ESC` ali não é caractere, é comando. Ele move o cursor, renomeia a janela, abre um hyperlink OSC-8. E um `\n` sozinho quebraria o layout de duas linhas, que é o contrato inteiro desta barra.
- **UTF-8 dos dois lados.** Rodando como subprocess (sem console anexado), o Python cai na codepage ANSI do sistema, cp1252 no Windows. Isso corrompe a saída (o `·` sai como byte cru) e a **entrada** (um nome de sessão com acento vira `implementaÃ§Ã£o`, o double-encode clássico). Por isso o script lê `sys.stdin.buffer` e decodifica UTF-8 explícito, e força `sys.stdout.reconfigure(encoding="utf-8")`. Se tu mexer nessa parte, confere por **byte**, não olhando pra tela - e não testa por pipe do PowerShell, ele re-encoda e falseia o resultado.
- **Leitura ao contrário dos `.jsonl`.** Transcript cresce; o script lê de trás pra frente em blocos de 256 KB e para assim que sai da janela de tempo. Antes de abrir qualquer arquivo ele filtra por `mtime`: a varredura semanal só toca no que foi modificado dentro da janela.
- **A varredura é recursiva.** Transcript de subagente e de workflow não fica ao lado do da sessão: vai pra subpasta. Se tu orquestra com subagentes, a maior parte do teu consumo mora lá - numa medição real, **63%** do volume. Varrer só o primeiro nível inflava a fatia do modelo caro, porque o denominador perdia justamente o trabalho barato delegado.
- **Cache de 10 minutos** pra fatia semanal, em `~/.claude/statusline-week-share.json`, invalidado quando o dia vira. Sem ele a varredura rodaria a cada refresh. Ele mora no teu próprio diretório em vez da pasta temporária do sistema, de propósito: em máquina compartilhada o `/tmp` é de todo mundo, um nome fixo colide entre usuários e abre o vetor clássico do symlink plantado. A escrita é atômica (arquivo temporário + `os.replace`), porque várias sessões do Claude Code renderizam ao mesmo tempo e escrevem esse mesmo arquivo.
- **Deduplicação de streaming:** entradas parciais de streaming seriam contadas duas vezes; o script guarda só as que têm `stop_reason` preenchido (mais a última, se ela vier como `null`).
- **Datas do transcript.** O timestamp vem com sufixo `Z`, que o `datetime.fromisoformat` só passou a aceitar no Python 3.11. No 3.8-3.10 isso viraria todo timestamp em `None` **em silêncio**: os tetos diários ficariam em 0.0% com a barra inteira parecendo funcionar. O script normaliza o sufixo e mantém um fallback por `strptime`.

### Diagnóstico

Liga o `CLAUDE_STATUSLINE_DEBUG=1` e cada render grava o payload que recebeu em `~/.claude/statusline-payload.json`. É o jeito mais rápido de descobrir quais campos o Claude Code manda, ou não manda, na tua versão.

Vem **desligado** de propósito: o payload carrega o nome da tua sessão e caminhos do teu disco, e gravar isso a cada 15 segundos é decisão tua, não default de biblioteca.

---

## Testes

```bash
python statusline.py --selftest     # ~200 checks internos, 0 dependências
```

Cobrem formatação de duração e de token, os termômetros, inferência da janela de contexto, a aritmética dos dois tetos (semana limpa, dia anterior estourado, véspera do reset, saldo zerado), decodificação do payload e a montagem das duas linhas. E mais, com arquivo temporário de verdade: o leitor reverso, a deduplicação de streaming, a janela de tempo, o custo ponderado e o cache em disco.

Pra ver um render real com o teu próprio payload:

```python
import subprocess, pathlib
raw = (pathlib.Path.home() / '.claude' / 'statusline-payload.json').read_bytes()
print(subprocess.run(['python', 'statusline.py'], input=raw, capture_output=True).stdout.decode('utf-8'))
```

(Esse arquivo só existe depois que tu ligou o `CLAUDE_STATUSLINE_DEBUG=1`.)

---

## Licença

MIT - ver [LICENSE](LICENSE).
