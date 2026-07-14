# Sexy Prime Bot ADS integrado

O bot ficou dedicado às modelos vinculadas, cadastro de destinos e envio dos anúncios aprovados pelo site.

## O que acontece no bot

- `/start`: consulta a vinculação do Telegram com o perfil da modelo.
- Link `start=link_TOKEN`: conclui a vinculação criada no Editar Perfil.
- A primeira vinculação válida recebe o bônus configurado no site (padrão: 5 créditos).
- Modelo vinculada envia foto, vídeo ou texto, legenda e botão/URL.
- O anúncio entra como pendente no painel administrativo do site.
- O bot só distribui anúncios aprovados e agendados pelo painel.
- Ao ser adicionado a grupos/canais, cadastra o destino como pendente para aprovação.
- `/registrar`: força o cadastro do grupo/canal atual.

## Configuração

1. Copie `.env.example` para `.env`.
2. Preencha `BOT_TOKEN` e `OWNER_ID`.
3. Em `SITE_API_SECRET`, use exatamente o segredo existente em `public_html/config/bot_ads.php`.
4. Localmente, use `RUN_MODE=polling`.
5. No Render, use `RUN_MODE=webhook` e configure `WEBHOOK_URL` com a URL pública do serviço.

O banco SQLite local guarda apenas o ID da última postagem em cada destino para a função "apagar anterior". Pontos, créditos, anúncios, aprovações, destinos e logs ficam no MySQL do site.
