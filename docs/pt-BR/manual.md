# Manual do usuário do MoonKobra

[English](../en/manual.md) · Português (Brasil)

Guia do dia a dia para usar o MoonKobra depois de instalado. Para instalar,
veja o [README](../../README.pt-BR.md). Para integrar outros programas, veja a
[referência da API](../api.md) (em inglês).

---

## Primeiros passos

### Instalar e iniciar

Siga o [Começo rápido](../../README.pt-BR.md#começo-rápido) do README. A versão
curta é:

```bash
./start.sh
```

e depois abrir `http://IP-DO-HOST:7125` no navegador.

### Primeiro acesso

O login inicial é `kx` / `kx123`. Na primeira entrada o MoonKobra pede uma
senha nova, e nada mais funciona até ela ser trocada. Dá para mudar usuário e
senha depois em **Ajustes → Sistema → Acesso / Login**, ou desligar o login
(não recomendado se a rede não for só sua).

### Conectar à impressora

1. Na tela da impressora: **Configurações → Ativar modo LAN**.
2. No MoonKobra, abra **Impressoras** e clique em **Adicionar impressora**.
   Digite o IP da impressora e confirme. Usuário, senha, ID do dispositivo e o
   certificado TLS são lidos da própria impressora; não é preciso digitar
   nada à mão.
3. O MoonKobra reinicia e conecta sozinho.

O certificado da impressora fica salvo em `config/certs/`. Se um dia a
conexão der erro de certificado, apague essa pasta e reinicie o MoonKobra:
ele busca um novo.

### Conectar o OrcaSlicer

No OrcaSlicer, crie a impressora com conexão do tipo **Moonraker** e host
`http://IP-DO-HOST:7125` (com `http://` e a porta). Cole a chave de
**Ajustes → API** no campo de chave de API. Para o fatiador reconhecer a marca
do filamento de cada slot, veja o
[Fatiador recomendado](../../README.pt-BR.md#fatiador-recomendado).

---

## Agora (tela principal)

A tela **Agora** mostra a impressão em andamento num relance:

- **Progresso**: a peça em 3D faz o papel de barra de progresso. Até a camada
  atual ela aparece sólida; acima, em holograma. Ao lado ficam porcentagem,
  camada, altura Z, tempo decorrido e restante.
  - Durante a impressão: **Pausar**, **Objetos** (pular objetos de uma mesa
    com várias peças) e **Segure para parar**. Parar exige segurar o botão
    por 1,4 s, para não acontecer sem querer.
  - Com um arquivo carregado e ainda parado: **Imprimir**, **Atribuir slots**
    e **Limpar**.
- **Trajetória**: as fases que a impressora informa (verificação, aquecimento,
  nivelamento, impressão, concluída).
- **Boletim do Moko**: o mascote comenta o estado da impressora (aquecendo,
  nivelando, esperando filamento, terminou, dormindo quando ela está
  desligada).
- **Temperaturas**: bico e mesa com valor atual e alvo, a tendência e um
  gráfico do histórico. **Definir** e **Desligar** para cada um.
- **Velocidade**: Silencioso, Normal e Sport (os modos da própria impressora).
- **Ventoinha**: slider e atalhos de 0 a 100%.
- **Eixos**: movimento em X/Y e Z com passo de 0,1 / 1 / 5 / 10 mm, **Home
  geral** e **Soltar motores**.
- **Câmera**: imagem ao vivo, com o botão da **Luz** no próprio card.
- **Filamento**: os slots do ACE, cada um com cor, material e perfil. Clique
  num slot para editar (veja [Filamento](#filamento)). O secador de cada
  unidade ACE aparece aqui também.

No topo ficam os modos de exibição **Dia**, **Noite** e **Madrugada**
(vermelho, para quarto escuro).

Quando a impressora pausa sozinha (filamento acabou, erro), um aviso mostra o
motivo em texto, e não só o código de erro.

---

## Imprimindo

### Enviar G-code

Em **Arquivos → Enviados**, arraste um `.gcode`/`.bgcode` para a área de envio
ou clique nela para escolher. Arquivos enviados pelo OrcaSlicer aparecem aqui
também. A lista tem miniaturas, busca, filtro (Todos / Concluídos / Com falha /
Novos), ordenação (data, nome, duração) e seleção múltipla para apagar em
lote. A aba **Na impressora** mostra os arquivos guardados na memória da
própria impressora.

Depois do envio, o que acontece depende de **Ajustes → Impressão → Depois do
upload**: um **diálogo de impressão** abre na hora, ou uma **faixa** fica no
topo da tela com as mesmas opções.

### Atribuir filamento e iniciar

Em arquivos que usam vários filamentos, o diálogo de atribuição abre sozinho
(ou pelo botão **Atribuir slots**). Nele você:

- liga cada filamento do G-code a um slot do ACE, com aviso quando o material
  ou a cor do slot não bate com o esperado;
- desmarca objetos em **Pular objetos**, antes de começar;
- liga ou desliga o nivelamento automático para esta impressão;
- escolhe o carretel do Spoolman de cada slot, se o Spoolman estiver
  configurado.

Confirme com **Imprimir**.

### Padrões de impressão

Em **Ajustes → Impressão**:

- **Slot padrão (cor única)**: automático (todos os ocupados) ou um slot fixo.
- **Auto-nivelamento** e **Compensação de ressonância** antes de imprimir.
- **Depois do upload**: diálogo ou faixa, como acima.
- **Ligar a câmera ao iniciar a impressão**.
- **Avisar em impressões enviadas pela web**: pede uma confirmação a mais,
  para pegar arquivo fatiado para a impressora errada.
- **Apagar o arquivo da impressora após imprimir**: limpa a memória dela
  quando a impressão termina bem (o arquivo continua no MoonKobra).

---

## Filamento

### Slots do ACE

Clique num slot na tela **Agora** para editar:

- **Cor**: seletor de cor, cores recentes ou copiar a cor de outro slot.
- **Material**: atalhos para os materiais comuns ou texto livre.
- **Perfil do OrcaSlicer**: o perfil que vai para o fatiador na sincronização,
  em vez do genérico "Generic PLA".
- **Carregar / descarregar** o filamento do slot.

O secador do ACE tem presets (PLA, PLA+, PETG, TPU, ABS/ASA, PA/PC e três
próprios com nome livre), cada um com temperatura e tempo. Os presets podem
ser editados e salvos.

### Perfis do OrcaSlicer

Em **Ajustes → Filamento → Perfis do OrcaSlicer**, clique em **Importar
perfis** e envie um ZIP da pasta de filamentos do OrcaSlicer (**Ajuda → Mostrar
pasta de configuração → user/<id>/filament/**) ou arquivos `.json` soltos. Os
perfis importados aparecem no grupo "★ Meus perfis" da lista de cada slot. Os
perfis brasileiros de `profiles/brasil/filamentos-brasil.zip` se importam do
mesmo jeito.

Ao criar um perfil próprio no OrcaSlicer para usar aqui, salve-o como
compatível com a **Anycubic Kobra X 0.4 nozzle** e reinicie o OrcaSlicer uma
vez depois de salvar: só então ele grava o `filament_id` do perfil, que é o que
faz o slot reconhecê-lo na sincronização.

Ainda em **Ajustes → Filamento**:

- **Perfil por slot**: fixa um perfil em cada slot, independente do que está
  carregado.
- **Fabricantes visíveis**: limita os fabricantes da lista de perfis. "Generic"
  e os seus perfis importados aparecem sempre.

### Spoolman

Em **Ajustes → Integrações → Spoolman**, informe a URL do servidor (ex.:
`http://spoolman:7912`) e a sincronização em segundos (`0` = só no fim da
impressão). Depois disso, **Ajustes → Filamento → Spoolman · carretel por
slot** liga cada slot a um carretel, e o consumo é descontado enquanto você
imprime.

---

## Orçamento (beta)

A aba **Orçamento** ainda está em desenvolvimento: confira os valores antes de
mandar ao cliente.

1. **Calcular**: escolha um G-code da lista e veja a peça em 3D, o tempo, o
   peso e as trocas de cor. Ajuste o pedido: quantidade, **peças por mesa**,
   canal de venda (venda direta, cartão, Shopee, Mercado Livre), imposto,
   cupom, frete, adicionais, modelagem, desconto e urgência. O preço aparece
   como uma pilha: material, purga do ACE, energia, máquina, mão de obra,
   reserva para falhas, embalagem, frete, taxas, imposto e, no topo, o seu
   lucro. Os custos com quadradinho podem ser tirados só deste pedido. O total
   pode ser editado à mão; as taxas e o lucro se refazem sobre ele.
2. **Salvar** guarda o orçamento com um número (`MK-AAAA-NNNN`). **Recibo**
   gera o recibo para o cliente em imagem, PDF ou texto para WhatsApp. O
   recibo nunca mostra custo interno nem margem.
3. **Orçamentos**: a lista dos salvos, para reabrir, gerar o recibo de novo ou
   apagar.
4. **Configurar**: impressora e energia, mão de obra e risco, margem e
   arredondamento, dados do seu negócio (vão no recibo), cupons, materiais,
   canais de venda, impostos e desconto por quantidade. As taxas mudam:
   revise de tempos em tempos (a data da última revisão aparece no topo).

---

## Várias impressoras

- **Adicionar**: em **Impressoras**, **Adicionar impressora** com o IP. Cada
  impressora ganha a própria porta (7126, 7127, …).
- **Trocar**: pelo seletor no cabeçalho ou pelo card na aba **Impressoras**,
  que também mostra o estado ao vivo de cada uma e quem está conectado nela.
- **Remover**: o **✕** no card da impressora, com confirmação.

---

## Interruptor de energia

O MoonKobra pode ligar e desligar uma **tomada inteligente** (por exemplo, com
Tasmota) ligada entre a parede e a fonte da impressora. Em **Ajustes →
Conexão → Interruptor de energia**, informe três URLs:

- **URL para ligar**
- **URL para desligar**
- **URL de status** (para mostrar se está ligada)

Num Tasmota costumam ser:

```
http://192.168.x.x/cm?cmnd=Power%20on
http://192.168.x.x/cm?cmnd=Power%20off
http://192.168.x.x/cm?cmnd=Power
```

com o IP da tomada, não o da impressora. O botão de energia aparece no card da
impressora em **Impressoras**. Desligar pede confirmação, porque corta a
energia de tudo que estiver na tomada.

---

## Notificações

O MoonKobra avisa quando uma impressão **termina**, **falha** ou **pausa por
erro** (filamento acabou, entupimento, aquecimento). Pausa feita por você não
gera aviso. Em **Ajustes → Conexão → Notificações**, informe uma URL; o
formato é escolhido pelo endereço:

- **ntfy**: `https://ntfy.sh/seu-topico` (ou um servidor ntfy próprio).
- **Discord**: a URL do webhook do canal.
- **Telegram**: `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>`.
- **Qualquer outra URL** recebe um POST em JSON com `printer`, `message`,
  `state` e `filename` (Home Assistant, Node-RED etc.).

Os textos saem no idioma da interface no momento em que você salvou.

---

## Ajustes

- **Conexão**: nome, IP, porta MQTT, usuário e senha MQTT, Device ID e Mode ID
  (preenchidos por "Adicionar impressora"), interruptor de energia e
  notificações.
- **Impressão**: os padrões descritos em [Imprimindo](#imprimindo).
- **Aparência**: idioma, modo de exibição, intervalo de consulta à impressora,
  log detalhado de requisições HTTP e quantos logs de impressão guardar.
- **Filamento**: perfis do OrcaSlicer, perfil por slot, fabricantes visíveis e
  Spoolman por slot.
- **Integrações**: câmera para OBS (links só da câmera, com token próprio),
  Spoolman e Obico.
- **API**: a chave de API (gerar e copiar) e um guia rápido com exemplos.
- **Sistema**: login, **backup da configuração** (um `.zip` com conexão,
  login, filamentos, Spoolman, configuração do Orçamento e perfis importados;
  guarde com cuidado, porque tem a senha da impressora) e a versão.

A maioria das mudanças vale depois de **Salvar e reiniciar**.

---

## Registro e diagnóstico

- **Registro → Ao vivo**: o log de eventos, com filtros por direção (RX/TX),
  nível (erros/avisos), tópico e texto, e o botão **Baixar**.
- **Registro → Histórico**: cada impressão guarda o próprio log, comprimido.
  Abra para ler ou baixe em `.txt`.
- **Erro de certificado / TLS**: o MoonKobra já busca outro certificado quando
  o salvo é recusado. Se continuar sem conectar, apague `config/certs/` e
  reinicie.
- **"Credenciais MQTT erradas"**: adicione a impressora de novo ou veja a
  [Solução de problemas](../../README.pt-BR.md#solução-de-problemas) do README.
- **Impressora não encontrada**: o modo LAN precisa estar ligado, e a
  impressora e o MoonKobra precisam estar na mesma rede.

---

## Câmera e Obico

- **Câmera**: o card Câmera mostra a imagem ao vivo da impressora, sem
  configuração. Para o OBS, use os links de **Ajustes → Integrações → Câmera
  para OBS**.
- **Obico**: a detecção de falhas e o time-lapse rodam pelo container
  `moonraker-obico`, configurado no arquivo `moonraker-obico.cfg`. O
  [`docker-compose.stack.yml`](../../docker-compose.stack.yml) já sobe tudo
  junto.
