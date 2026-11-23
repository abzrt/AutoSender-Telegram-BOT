<?php
/**
 * Webhook opsional untuk cPanel agar perintah /start dan /chatid bisa digunakan.
 *
 * Cara aktifkan webhook (buka sekali di browser setelah upload ke HTTPS):
 * https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://domainkamu.com/bot/webhook.php
 */

declare(strict_types=1);

$config = require __DIR__ . '/config.php';
$token = trim((string)($config['bot_token'] ?? ''));

$raw = file_get_contents('php://input');
$update = json_decode((string)$raw, true);

if (!is_array($update) || empty($update['message'])) {
    http_response_code(200);
    exit('OK');
}

$message = $update['message'];
$chatId = $message['chat']['id'] ?? null;
$chatType = $message['chat']['type'] ?? 'unknown';
$userId = $message['from']['id'] ?? 'N/A';
$text = trim((string)($message['text'] ?? ''));

if ($chatId === null) {
    exit('OK');
}

$reply = null;
if (str_starts_with($text, '/start') || str_starts_with($text, '/help')) {
    $reply = "<b>Telegram AutoSender (cPanel PHP) aktif!</b>\n\n"
        . "Chat ID ini: <code>{$chatId}</code>\n"
        . "Tipe Chat: <code>{$chatType}</code>\n"
        . "User ID kamu: <code>{$userId}</code>\n\n"
        . "Gunakan <code>/chatid</code> di grup/chat tujuan untuk melihat Chat ID.";
} elseif (str_starts_with($text, '/chatid')) {
    $reply = "Chat ID: <code>{$chatId}</code>\nTipe: <code>{$chatType}</code>";
}

if ($reply !== null && $token !== '' && $token !== 'ISI_TOKEN_BOT_DI_SINI') {
    $ch = curl_init("https://api.telegram.org/bot{$token}/sendMessage");
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => [
            'chat_id' => $chatId,
            'text' => $reply,
            'parse_mode' => 'HTML',
        ],
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 15,
    ]);
    curl_exec($ch);
    curl_close($ch);
}

http_response_code(200);
echo 'OK';
