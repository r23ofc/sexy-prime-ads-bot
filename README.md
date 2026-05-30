# Sexy Prime Ads Bot

Bot de anúncios para a Agência Sexy Prime com painel pelo Telegram, mídia ou anúncio somente texto, botão URL, texto citado, links embutidos, agendamento, fixação, exclusão da última postagem, destinos aprovados e modo Render/Webhook.

## Funções

- Painel exclusivo para dono/admin
- Usuário comum recebe aviso de uso exclusivo
- Criar anúncio com foto, vídeo ou somente texto
- Aceita textos citados/blockquote do Telegram, links embutidos e mensagens de texto com redirecionamento
- Descrição + botão URL
- Prévia antes de postar
- Postar agora
- Agendar por horário
- Postagem automática por intervalo: 1h, 2h, 3h, 4h, 6h ou 12h
- Fixar anúncio automaticamente
- Apagar postagem anterior do bot
- Aprovar/rejeitar grupos e canais
- Logs de erro
- Backup do banco pelo Telegram
- Modo local com polling
- Modo Render com webhook
- Endpoint de ping para cron de 10 em 10 minutos

---

## Arquivos

```txt
bot.py
ping.py
requirements.txt
.env.example
render.yaml
rodar_windows.bat
sexy-prime-ads.service.example
data/
logs/
```

---

## Rodar localmente no Windows

### 1. Instale as dependências

```bat
cd C:\xampp\htdocs\sexy_prime_ads_bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Crie o arquivo `.env`

Copie `.env.example` e renomeie para `.env`.

Para rodar localmente, use:

```env
RUN_MODE=polling
BOT_TOKEN=SEU_TOKEN
OWNER_ID=SEU_ID
AGENCY_NAME=Sexy Prime
SUPPORT_URL=https://t.me/SXP_suporte
TIMEZONE=America/Sao_Paulo
DB_PATH=data/sexy_prime_ads.db
```

### 3. Rode

```bat
python bot.py
```

---

## Rodar no Render

### 1. Envie o projeto para o GitHub

Suba estes arquivos para um repositório GitHub.

### 2. Crie um Web Service no Render

No Render:

```txt
New > Web Service > selecione o repositório
```

Configuração:

```txt
Runtime: Python
Build Command: pip install -r requirements.txt
Start Command: python bot.py
Plan: Free
```

### 3. Configure as variáveis no Render

Em **Environment**, adicione:

```env
RUN_MODE=webhook
BOT_TOKEN=TOKEN_DO_BOT
OWNER_ID=SEU_ID_NUMERICO
AGENCY_NAME=Sexy Prime
SUPPORT_URL=https://t.me/SXP_suporte
TIMEZONE=America/Sao_Paulo
DB_PATH=data/sexy_prime_ads.db
WEBHOOK_URL=https://SEU-SERVICO.onrender.com
WEBHOOK_PATH=/webhook/sexy-prime-ads-segredo
```

Troque `SEU-SERVICO` pelo nome real do seu serviço no Render.

Exemplo:

```env
WEBHOOK_URL=https://sexy-prime-ads-bot.onrender.com
WEBHOOK_PATH=/webhook/sp-ads-928371-secreto
```

A URL final do webhook/ping será:

```txt
https://sexy-prime-ads-bot.onrender.com/webhook/sp-ads-928371-secreto
```

---

## Cron Job de 10 em 10 minutos

O bot aceita `GET` no próprio webhook. Então o cron pode acessar esta URL de 10 em 10 minutos:

```txt
https://SEU-SERVICO.onrender.com/webhook/SEU-CAMINHO-SECRETO
```

Também funcionam:

```txt
https://SEU-SERVICO.onrender.com/
https://SEU-SERVICO.onrender.com/health
https://SEU-SERVICO.onrender.com/ping
```

### Opção A: Cron externo grátis

Use um serviço como `cron-job.org` ou UptimeRobot.

Configuração:

```txt
URL: https://SEU-SERVICO.onrender.com/webhook/SEU-CAMINHO-SECRETO
Método: GET
Intervalo: 10 minutos
```

### Opção B: Cron Job dentro do Render

O arquivo `ping.py` foi criado para isso.

Crie um novo serviço no Render:

```txt
New > Cron Job
```

Configuração:

```txt
Runtime: Python
Build Command: pip install -r requirements.txt
Command: python ping.py
Schedule: */10 * * * *
```

Variável:

```env
PING_URL=https://SEU-SERVICO.onrender.com/webhook/SEU-CAMINHO-SECRETO
```

Atenção: Cron Job do Render pode ter cobrança mínima mensal. Para grátis, prefira cron externo.



---

## Anúncios somente texto, texto citado e links embutidos

Ao criar um anúncio, depois do título você pode enviar:

```txt
1. Foto
2. Vídeo
3. Mensagem de texto pronta
```

Se enviar uma mensagem de texto pronta, o anúncio será salvo como **somente texto**. O bot preserva formatações feitas no próprio Telegram, como:

```txt
- Negrito
- Itálico
- Código
- Spoiler
- Texto citado / blockquote
- Link embutido em palavra ou frase
- Link normal no texto
```

Exemplo de anúncio de texto:

```txt
🔥 Sexy Prime VIP

> Oferta exclusiva de hoje

Clique no botão abaixo para acessar.
```

Depois disso, o bot ainda pergunta se você quer adicionar um botão URL separado, por exemplo:

```txt
Texto do botão: Entrar agora
URL: https://t.me/seulink
```

---

## Postagem automática de 3 em 3 horas

Depois de criar um anúncio:

```txt
/start > 📋 Meus anúncios > escolha o anúncio > 🔁 Automático > 3 em 3 horas
```

O bot vai postar esse anúncio automaticamente em todos os destinos aprovados ativos a cada 3 horas.

Importante:

- A primeira postagem automática acontece depois do intervalo escolhido.
- Para postar imediatamente, use `🚀 Postar agora`.
- Se ativar outro intervalo para o mesmo anúncio, o intervalo antigo daquele anúncio é parado automaticamente.
- Para parar, use `🔁 Postagem automática` no painel e clique em `⛔ Parar automático`.

---

## Comandos do bot

```txt
/start - abre painel
/panel - abre painel admin
/id - mostra seu ID
/help - ajuda
/addadmin ID - adiciona admin extra
/removeadmin ID - remove admin extra
/backup - baixa backup do banco
/cancel - cancela operação atual
```

---

## Permissões necessárias

Para postar em grupo/canal, coloque o bot como admin com permissões:

- Enviar mensagens
- Apagar mensagens
- Fixar mensagens
- Postar mensagens em canal

---

## Aviso importante sobre SQLite no Render Free

O banco padrão é SQLite em `data/sexy_prime_ads.db`.

No Render Free, o sistema de arquivos é temporário. Se o serviço reiniciar, redeployar ou dormir, o banco pode ser perdido. Para teste funciona, mas para produção use:

- Oracle Cloud Always Free com SQLite/arquivo persistente; ou
- Render pago com persistent disk; ou
- Postgres externo.



## Notificações de postagem automática

Por padrão, a postagem automática por intervalo não envia mensagem no PV do dono a cada execução.

Para mudar isso no Render, use:

```env
NOTIFY_INTERVAL_POSTS=false
NOTIFY_SCHEDULED_POSTS=true
```

Se quiser receber relatório no PV toda vez que a postagem automática rodar, altere `NOTIFY_INTERVAL_POSTS` para `true`.

---

## Atualizar o bot sem remover dos grupos/canais

Esta versão adiciona os comandos:

```txt
/registrar
/sincronizar
```

Use `/registrar` dentro de um grupo ou canal onde o bot já está. O bot vai registrar ou atualizar aquele destino no banco sem você precisar remover e adicionar novamente.

Fluxo recomendado:

```txt
1. Faça deploy da nova versão no Render.
2. Abra o grupo/canal onde o bot já está.
3. Envie /registrar.
4. No painel do dono, aprove o destino se ele aparecer como pendente.
```

Se o comando for enviado por um dono/admin do bot dentro de um grupo, o destino é aprovado automaticamente. Em canal, o destino pode ficar como pendente porque o Telegram não informa o usuário que postou no canal.

Use `/sincronizar` no privado do bot para atualizar nome, tipo, status e permissão de fixação dos destinos que já existem no banco.

Importante: a API do Telegram não permite que o bot descubra sozinho todos os grupos e canais onde ele já está. Se o banco SQLite do Render for apagado em redeploy/restart, será necessário enviar `/registrar` uma vez dentro de cada grupo/canal. Para não perder destinos entre deploys, use banco persistente externo ou uma hospedagem com disco persistente.
