<?php
/**
 * Telegram AutoSender Bot (PHP Standalone untuk cPanel Cron Job).
 *
 * Cara jalankan via cPanel Cron Job (setiap menit):
 *   * * * * * /usr/local/bin/php /home/USERNAME/telegram-autosender/autosender.php >/dev/null 2>&1
 *
 * Mode tes (CLI):
 *   php autosender.php --dry-run
 *   php autosender.php --force=sapaan_pagi
 */

declare(strict_types=1);

$config = require __DIR__ . '/config.php';
date_default_timezone_set($config['timezone'] ?? 'Asia/Jakarta');

$isCli = (PHP_SAPI === 'cli');
$dryRun = false;
$forceJobId = null;

// Keamanan untuk eksekusi via HTTP/URL
if (!$isCli) {
    header('Content-Type: text/plain; charset=utf-8');
    $providedKey = (string)($_GET['key'] ?? '');
    $expectedKey = (string)($config['cron_secret'] ?? '');

    if ($expectedKey === '' || $expectedKey === 'GANTI_DENGAN_KODE_RAHASIA_ACAK' || !hash_equals($expectedKey, $providedKey)) {
        http_response_code(403);
        echo "403 Forbidden: key tidak valid atau cron_secret belum diganti.\n";
        exit(1);
    }

    $dryRun = isset($_GET['dry_run']) && $_GET['dry_run'] === '1';
    $forceJobId = isset($_GET['force']) ? trim((string)$_GET['force']) : null;
} else {
    foreach ($argv as $arg) {
        if ($arg === '--dry-run' || $arg === '--check') {
            $dryRun = true;
        } elseif (str_starts_with($arg, '--force=')) {
            $forceJobId = trim(substr($arg, strlen('--force=')));
        }
    }
}

function writeLog(string $logFile, string $message): void
{
    $line = '[' . date('Y-m-d H:i:s') . '] ' . $message . PHP_EOL;
    echo $line;
    @file_put_contents($logFile, $line, FILE_APPEND | LOCK_EX);
}

/**
 * Cocokkan satu field cron (*, N, A-B, * /S, A-B/S, atau daftar dipisah koma).
 */
function matchCronField(string $expr, int $value, int $min, int $max): bool
{
    foreach (explode(',', trim($expr)) as $part) {
        $part = trim($part);
        if ($part === '*') {
            return true;
        }

        $step = 1;
        if (str_contains($part, '/')) {
            [$range, $stepStr] = explode('/', $part, 2);
            $step = max(1, (int)$stepStr);
        } else {
            $range = $part;
        }

        if ($range === '*') {
            if (($value - $min) % $step === 0) {
                return true;
            }
            continue;
        }

        if (str_contains($range, '-')) {
            [$startStr, $endStr] = explode('-', $range, 2);
            $start = (int)$startStr;
            $end = (int)$endStr;
            if ($value >= $start && $value <= $end && (($value - $start) % $step === 0)) {
                return true;
            }
            continue;
        }

        if ((int)$range === $value) {
            return true;
        }
    }

    return false;
}

/**
 * Cek apakah ekspresi cron 5-bagian (menit jam tanggal bulan hari_minggu) cocok dengan waktu sekarang.
 * Catatan: hari_minggu 0 atau 7 = Minggu, 1 = Senin, ..., 6 = Sabtu.
 */
function isCronDue(string $cronExpr, DateTimeImmutable $now): bool
{
    $parts = preg_split('/\s+/', trim($cronExpr));
    if (!$parts || count($parts) !== 5) {
        return false;
    }

    [$minExpr, $hourExpr, $dayExpr, $monthExpr, $dowExpr] = $parts;
    // Normalisasi hari Minggu (7 -> 0)
    $dowExpr = preg_replace('/\b7\b/', '0', $dowExpr) ?? $dowExpr;

    return matchCronField($minExpr, (int)$now->format('i'), 0, 59)
        && matchCronField($hourExpr, (int)$now->format('G'), 0, 23)
        && matchCronField($dayExpr, (int)$now->format('j'), 1, 31)
        && matchCronField($monthExpr, (int)$now->format('n'), 1, 12)
        && matchCronField($dowExpr, (int)$now->format('w'), 0, 6);
}

/**
 * Kirim request ke Telegram Bot API menggunakan cURL.
 */
function telegramRequest(string $token, string $method, array $payload): array
{
    $url = "https://api.telegram.org/bot{$token}/{$method}";
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => $payload,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_TIMEOUT => 30,
    ]);

    $response = curl_exec($ch);
    $curlErr = curl_error($ch);
    $httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($response === false) {
        return ['ok' => false, 'error' => "cURL error: {$curlErr}"];
    }

    $decoded = json_decode($response, true);
    if (!is_array($decoded)) {
        return ['ok' => false, 'error' => "HTTP {$httpCode}: respons bukan JSON"];
    }

    return $decoded;
}

/**
 * Kirim pesan/foto dari satu job ke semua chat_ids.
 */
function sendJob(string $token, array $job, string $logFile, bool $dryRun): bool
{
    $chatIds = $job['chat_ids'] ?? [];
    $message = (string)($job['message'] ?? '');
    $photo = $job['photo'] ?? null;
    $parseMode = $job['parse_mode'] ?? 'HTML';
    $allOk = true;

    foreach ($chatIds as $chatId) {
        if ($dryRun) {
            writeLog($logFile, "[DRY-RUN] Akan mengirim job '{$job['id']}' ke chat_id={$chatId}");
            continue;
        }

        if ($photo) {
            $payload = [
                'chat_id' => $chatId,
                'photo' => is_file((string)$photo) ? new CURLFile((string)$photo) : (string)$photo,
            ];
            if ($message !== '') {
                $payload['caption'] = $message;
            }
            if ($parseMode) {
                $payload['parse_mode'] = $parseMode;
            }
            $res = telegramRequest($token, 'sendPhoto', $payload);
        } else {
            $payload = [
                'chat_id' => $chatId,
                'text' => $message,
            ];
            if ($parseMode) {
                $payload['parse_mode'] = $parseMode;
            }
            $res = telegramRequest($token, 'sendMessage', $payload);
        }

        if (!empty($res['ok'])) {
            writeLog($logFile, "OK: Job '{$job['id']}' terkirim ke chat_id={$chatId}");
        } else {
            $err = $res['description'] ?? $res['error'] ?? 'Unknown error';
            writeLog($logFile, "GAGAL: Job '{$job['id']}' ke chat_id={$chatId} -> {$err}");
            $allOk = false;
        }

        usleep(50000); // 50ms jeda aman dari rate limit
    }

    return $allOk;
}

// --- Mulai eksekusi utama ---
$logFile = $config['log_file'];
$token = trim((string)($config['bot_token'] ?? ''));

if (!$dryRun && ($token === '' || $token === 'ISI_TOKEN_BOT_DI_SINI')) {
    writeLog($logFile, 'ERROR: bot_token belum diisi di config.php.');
    exit(1);
}

$schedulesFile = $config['schedules_file'];
if (!is_file($schedulesFile)) {
    writeLog($logFile, "ERROR: File jadwal tidak ditemukan: {$schedulesFile}");
    exit(1);
}

$jobs = json_decode((string)file_get_contents($schedulesFile), true);
if (!is_array($jobs)) {
    writeLog($logFile, 'ERROR: Format schedules.json tidak valid.');
    exit(1);
}

$stateFile = $config['state_file'];
$state = is_file($stateFile) ? (json_decode((string)file_get_contents($stateFile), true) ?: []) : [];

$now = new DateTimeImmutable('now');
$nowTs = $now->getTimestamp();
$currentMinuteKey = $now->format('Y-m-d H:i');
$stateChanged = false;

foreach ($jobs as $job) {
    $jobId = (string)($job['id'] ?? '');
    if ($jobId === '') {
        continue;
    }

    $isForced = ($forceJobId !== null && $forceJobId === $jobId);
    $enabled = (bool)($job['enabled'] ?? true);

    if (!$enabled && !$isForced) {
        continue;
    }

    $shouldRun = $isForced;
    $trigger = strtolower((string)($job['trigger'] ?? ''));

    if (!$shouldRun && $trigger === 'date') {
        $alreadySent = !empty($state[$jobId]['sent_once']);
        $runAtRaw = trim((string)($job['run_at'] ?? ''));
        if (!$alreadySent && $runAtRaw !== '') {
            $runDt = DateTimeImmutable::createFromFormat('Y-m-d H:i:s', $runAtRaw)
                ?: DateTimeImmutable::createFromFormat('Y-m-d H:i', $runAtRaw);
            if ($runDt !== false && $nowTs >= $runDt->getTimestamp()) {
                $shouldRun = true;
            }
        }
    } elseif (!$shouldRun && $trigger === 'cron') {
        $lastMinute = $state[$jobId]['last_minute'] ?? null;
        if ($lastMinute !== $currentMinuteKey && isCronDue((string)($job['cron'] ?? ''), $now)) {
            $shouldRun = true;
        }
    } elseif (!$shouldRun && $trigger === 'interval') {
        $intervalSeconds = ((int)($job['days'] ?? 0) * 86400)
            + ((int)($job['hours'] ?? 0) * 3600)
            + ((int)($job['minutes'] ?? 0) * 60)
            + (int)($job['seconds'] ?? 0);

        if ($intervalSeconds > 0) {
            $lastRunTs = (int)($state[$jobId]['last_run_ts'] ?? 0);
            if ($lastRunTs === 0 || ($nowTs - $lastRunTs) >= $intervalSeconds) {
                $shouldRun = true;
            }
        }
    }

    if ($shouldRun) {
        sendJob($token, $job, $logFile, $dryRun);
        if (!$dryRun) {
            $state[$jobId] = [
                'last_run_ts' => $nowTs,
                'last_minute' => $currentMinuteKey,
                'sent_once' => ($trigger === 'date'),
            ];
            $stateChanged = true;
        }
    }
}

if ($stateChanged) {
    @file_put_contents($stateFile, json_encode($state, JSON_PRETTY_PRINT), LOCK_EX);
}
