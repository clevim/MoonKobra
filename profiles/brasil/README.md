# Perfis de filamento brasileiros

Perfis para a **Anycubic Kobra X (bico 0.4)**. Cada perfil herda o perfil
Anycubic equivalente do OrcaSlicer e só troca o que o fabricante publica:
temperatura e faixa do bico, mesa, densidade e ventoinha. A fonte de cada valor
fica em `filament_notes` dentro do JSON.

| Marca | Linhas | Cores |
|---|---|---|
| 3DFila | PLA, PLA Matte, PLA Silk (+ Duo, Tricolor, Rainbow), PLA Magic, PLA EasyFill, PLA Wood, PLA GF, PETG XT, PETG High Speed, TPU Flex, ABS Premium | 132 cores, um perfil por cor |
| GTMax3D | PLA, PLA Speed+, PETG, ABS Premium | só o perfil da linha |
| 3D Lab | PLA, PETG | só o perfil da linha |
| F3D | PLA Premium | só o perfil da linha |

> Nenhuma dessas marcas publica perfil pronto para o OrcaSlicer. As
> temperaturas vêm das fichas técnicas dos sites (set/2026); fluxo, retração e
> velocidade volumétrica vêm do perfil da Anycubic. Calibre (torre de
> temperatura e fluxo) e ajuste as tabelas em `gerar.py`.

## Cores da 3DFila

`3dfila_cores.json` tem as cores de todos os PLA, PETG e TPU do site da 3DFila,
com o estoque na data da coleta:

- **99 cores com HEX oficial**, lido das imagens "Pantone: … / HEX: …" de cada produto.
- **33 cores aproximadas**, tiradas da foto do carretel, porque a 3DFila não publica o HEX delas:
  PETG High Speed - Preto, PETG High Speed - Transparente, PETG XT - Branco Snow White, PETG XT - Branco Translúcido, PETG XT - ECO (cores mistas), PETG XT - Laranja, PETG XT - Prata, PETG XT - Preto Black Night, PETG XT - Transparente Glass Colorless, PETG XT - Verde, PLA - Amêndoa, PLA - ECO (cores mistas), PLA - Prata, PLA EasyFill - Amarelo Sunshine, PLA EasyFill - Azul Sky, PLA EasyFill - ECO (cores mistas), PLA Magic - Amarelo Neon, PLA Magic - ECO (cores mistas), PLA Silk - Amarelo, PLA Silk - Azul Claro, PLA Silk - Laranja, PLA Silk - Preto, PLA Silk - Rosa, PLA Silk - Verde, PLA Silk - Vermelho e Dourado, TPU Flex - Amarelo, TPU Flex - Azul Pedra, TPU Flex - Branco, TPU Flex - Cinza, TPU Flex - Preto, TPU Flex - Transparente, TPU Flex - Verde Bambu, TPU Flex - Vermelho.

A cor de cada perfil é sempre o **HEX escrito pela 3DFila** na imagem, mesmo
quando o fundo da própria imagem mostra um tom diferente (acontece em PLA
Amarelo, PLA Champanhe, PETG XT Cinza Gray Storm, PLA Silk Preto e Roxo, PLA
Wood Natural e numa das cores do PLA Matte Rainbow).

Filamentos Duo/Tricolor/Rainbow usam a primeira cor como cor do perfil; as outras
ficam listadas em `filament_notes`.

**Linhas fora dos perfis:** a 3DFila não publica ficha técnica de PLA HT, PLA
ABS-Like, PLA CF, HIPS, Antichama FRP, Condutivo e Antiviral, então essas não
entraram.

## Preço (3DFila)

Cada perfil de cor da 3DFila leva o **preço cheio do carretel de 1 kg** (sem
promoção) em `filament_cost`, em R$/kg. O perfil da linha leva o preço mais
comum entre as cores dela. Com isso o OrcaSlicer mostra o custo do filamento no
fatiamento, e o **Orçamento** do MoonKobra usa esse preço sozinho: ele lê no
G-code o nome do perfil de cada ferramenta e procura o perfil importado com o
mesmo nome. Material escolhido à mão na tela do Orçamento tem prioridade.

Para atualizar os preços (a 3DFila muda de tempos em tempos):

```
python3 profiles/brasil/precos_3dfila.py   # lê o site e grava preco_kg em 3dfila_cores.json
python3 profiles/brasil/gerar.py           # regera os perfis e o zip
```

Depois reimporte os perfis no OrcaSlicer e o `filamentos-brasil.zip` no MoonKobra.
A data da coleta fica em `filament_notes` ("Preço 1 kg: R$ 99,90 em …").

## Instalar no OrcaSlicer

Copie os arquivos de `orcaslicer/` para a pasta de filamentos de usuário e
reinicie o OrcaSlicer:

- **Linux:** `~/.config/OrcaSlicer/user/default/filament/`
- **Windows:** `%APPDATA%\OrcaSlicer\user\default\filament\`

Outra opção: **Arquivo → Importar → Importar configurações...** e escolher os `.json`.

Os perfis aparecem como "3DFila PLA" (linha) e "3DFila PLA - Azul Caneta" (cor).
Eles só aparecem com a Kobra X selecionada, porque o perfil pai é específico dela.

## Instalar no MoonKobra

Em **Configurações → Filamento → Perfis do OrcaSlicer**, arraste o
`filamentos-brasil.zip`. No dashboard, clique no slot do AMS e escolha a linha
ou a cor no dropdown de perfis: abaixo dele aparece a **paleta com todas as
cores daquela linha**. Clicar numa cor escolhe o perfil dela e já aplica a cor
no slot.

Instale **dos dois lados**: o bridge manda o nome e o fabricante do perfil, e o
OrcaSlicer só consegue casar com um preset que exista nele.

## Adicionar ou alterar

Edite `LINHAS` em `gerar.py` (temperaturas) ou `3dfila_cores.json` (cores) e rode:

```bash
python3 profiles/brasil/gerar.py
```

Os `filament_id` são derivados do nome ("P" + 7 hex), então renomear um perfil
muda o ID dele.
