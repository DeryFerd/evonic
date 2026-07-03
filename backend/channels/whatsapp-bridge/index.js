'use strict';

const express = require('express');
const pino = require('pino');
const QRCode = require('qrcode');
const fs = require('fs');
const path = require('path');

const PORT = parseInt(process.env.PORT || '3001', 10);
const CALLBACK_URL = process.env.CALLBACK_URL || '';
const CALLBACK_SECRET = process.env.CALLBACK_SECRET || '';
const AUTH_DIR = process.env.AUTH_DIR || './auth_info';

const logger = pino({ level: 'warn' });
const app = express();
app.use(express.json());

// Connection state
let sock = null;
let currentQR = null;
let connectionStatus = 'disconnected'; // 'disconnected' | 'qr_pending' | 'connected'
let isShuttingDown = false;
let botId = '';   // PN-based JID (e.g. 628xxx:1@s.whatsapp.net)
let botLid = '';  // LID-based JID (e.g. 123456:1@lid)
let lastPushedStatus = '';

function pushStatus() {
    if (!CALLBACK_URL || isShuttingDown) return;
    if (connectionStatus === lastPushedStatus) return;
    lastPushedStatus = connectionStatus;
    postCallback({ event: 'status', status: connectionStatus });
}

// Unwrap container messages (disappearing / view-once) to reach the real content.
// Groups with disappearing messages enabled wrap every message in ephemeralMessage.
function unwrapMessage(message) {
    return message?.ephemeralMessage?.message
        || message?.viewOnceMessage?.message
        || message?.viewOnceMessageV2?.message
        || message?.documentWithCaptionMessage?.message
        || message;
}

async function startBaileys() {
    const baileys = await import('@whiskeysockets/baileys');
    const {
        default: makeWASocket,
        useMultiFileAuthState,
        DisconnectReason,
        fetchLatestBaileysVersion,
        makeCacheableSignalKeyStore,
        downloadMediaMessage,
        areJidsSameUser,
    } = baileys;
    const makeInMemoryStore = baileys.makeInMemoryStore || null;
    const { Boom } = await import('@hapi/boom');
    const fs = await import('fs');

    fs.default.mkdirSync(AUTH_DIR, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();

    sock = makeWASocket({
        version,
        auth: {
            creds: state.creds,
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        printQRInTerminal: false,
        logger,
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
        if (qr) {
            currentQR = qr;
            connectionStatus = 'qr_pending';
            console.log('[whatsapp-bridge] QR generated — waiting for scan');
        }

        if (connection === 'open') {
            currentQR = null;
            connectionStatus = 'connected';
            botId = sock.user?.id || '';
            botLid = sock.user?.lid || '';
            console.log('[whatsapp-bridge] Connected to WhatsApp (id=%s, lid=%s)', botId, botLid);
        }

        if (connection === 'close') {
            connectionStatus = 'disconnected';
            const statusCode = lastDisconnect?.error?.output?.statusCode;
            const loggedOut = statusCode === DisconnectReason.loggedOut;

            if (isShuttingDown) return;

            if (loggedOut) {
                console.log('[whatsapp-bridge] Logged out — clearing session and restarting');
                fs.default.rmSync(AUTH_DIR, { recursive: true, force: true });
                setTimeout(startBaileys, 3000);
            } else {
                console.log('[whatsapp-bridge] Disconnected, reconnecting...');
                setTimeout(startBaileys, 3000);
            }
        }

        pushStatus();
    });

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return;
        if (!CALLBACK_URL) return;

        for (const msg of messages) {
            if (msg.key.fromMe) continue;

            const from = msg.key.remoteJid || '';
            const isGroup = from.endsWith('@g.us');

            // In groups, remoteJid is the group; the actual sender is in participant
            const participant = isGroup ? (msg.key.participant || '') : '';
            const jid = from;
            const sender = isGroup
                ? (participant.includes('@') ? participant.split('@')[0] : participant)
                : (from.includes('@') ? from.split('@')[0] : from);
            const messageId = msg.key.id || '';
            const content = unwrapMessage(msg.message);

            // Extract text
            const text =
                content?.conversation ||
                content?.extendedTextMessage?.text ||
                content?.imageMessage?.caption ||
                content?.videoMessage?.caption ||
                '';

            // Extract button reply (approval flow)
            const buttonReply = content?.buttonsResponseMessage;
            if (buttonReply) {
                const buttonId = buttonReply.selectedButtonId || '';
                postCallback({ from: sender, jid, message_id: messageId, button_id: buttonId, text: '' });
                continue;
            }

            // Extract image if present
            let image = null;
            if (content?.imageMessage) {
                try {
                    // downloadMediaMessage unwraps ephemeral/view-once internally
                    const buffer = await downloadMediaMessage(msg, 'buffer', {}, { logger });
                    const mimetype = content.imageMessage.mimetype || 'image/jpeg';
                    image = {
                        base64: buffer.toString('base64'),
                        mimetype,
                    };
                } catch (e) {
                    console.error('[whatsapp-bridge] Failed to download image:', e.message);
                }
            }

            // Log every inbound message (early feature — verbose for monitoring)
            console.log('[whatsapp-bridge] MSG id=%s from=%s jid=%s group=%s text_len=%d image=%s',
                messageId, sender, jid, isGroup, text.length, !!image);
            if (isGroup) {
                console.log('[whatsapp-bridge] GROUP MSG keys:', JSON.stringify(Object.keys(msg.message || {})),
                    'unwrapped:', JSON.stringify(Object.keys(content || {})));
                console.log('[whatsapp-bridge] GROUP MSG from:', sender, 'text:', text?.substring(0, 100));
            }

            // Extract reply/quoted context (contextInfo lives on whichever message type is present)
            const contextInfo = content?.extendedTextMessage?.contextInfo
                || content?.imageMessage?.contextInfo
                || content?.videoMessage?.contextInfo
                || content?.audioMessage?.contextInfo;
            let quotedText = null;
            let quotedIsBot = false;
            const quoted = contextInfo?.quotedMessage;
            if (quoted) {
                quotedText =
                    quoted.conversation ||
                    quoted.extendedTextMessage?.text ||
                    null;
                const quotedParticipant = contextInfo.participant || '';
                if (quotedParticipant) {
                    quotedIsBot = (botId && areJidsSameUser(quotedParticipant, botId))
                        || (botLid && areJidsSameUser(quotedParticipant, botLid));
                } else {
                    console.log('[whatsapp-bridge] WARNING: quoted message without participant:', JSON.stringify(contextInfo));
                }
            }

            // Check if bot is @mentioned
            const mentionedJids = contextInfo?.mentionedJid || [];
            let botMentioned = mentionedJids.some(
                m => (botId && areJidsSameUser(m, botId))
                    || (botLid && areJidsSameUser(m, botLid))
            );
            // Fallback: check text for @bot_number if contextInfo didn't have mentions.
            // In LID-addressed groups the mention text carries the LID digits, not the phone.
            if (!botMentioned && isGroup && text) {
                const botPhone = botId ? botId.split(':')[0].split('@')[0] : '';
                const botLidDigits = botLid ? botLid.split(':')[0].split('@')[0] : '';
                if ((botPhone && text.includes('@' + botPhone)) ||
                    (botLidDigits && text.includes('@' + botLidDigits))) {
                    botMentioned = true;
                }
            }
            if (isGroup) {
                console.log('[whatsapp-bridge] mentionedJids:', JSON.stringify(mentionedJids), 'botMentioned:', botMentioned, 'quotedIsBot:', quotedIsBot, 'botId:', botId, 'botLid:', botLid);
            }

            postCallback({
                from: sender, jid, message_id: messageId, text, image,
                quoted_text: quotedText,
                is_group: isGroup,
                bot_mentioned: botMentioned,
                quoted_is_bot: quotedIsBot,
                pushName: msg.pushName || '',
            });
        }
    });
}

function postCallback(payload) {
    const http = require('http');
    const url = new URL(CALLBACK_URL);
    const body = JSON.stringify(payload);
    const req = http.request({
        hostname: url.hostname,
        port: url.port || 80,
        path: url.pathname,
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(body),
            'Authorization': `Bearer ${CALLBACK_SECRET}`,
        },
    }, (res) => {
        res.resume(); // drain
    });
    req.on('error', (e) => console.error('[whatsapp-bridge] Callback error:', e.message));
    req.write(body);
    req.end();
}

// ---- REST API ----

app.get('/status', (req, res) => {
    res.json({ status: connectionStatus });
});

app.get('/qr', async (req, res) => {
    if (connectionStatus === 'connected') {
        return res.json({ status: 'connected' });
    }
    if (!currentQR) {
        return res.json({ status: connectionStatus, qr: null });
    }
    try {
        const png = await QRCode.toDataURL(currentQR);
        res.json({ status: 'qr_pending', qr: png });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

app.post('/send', async (req, res) => {
    const { to, text } = req.body || {};
    if (!to || !text) return res.status(400).json({ error: 'to and text required' });
    if (!sock || connectionStatus !== 'connected') {
        return res.status(503).json({ error: 'Not connected to WhatsApp' });
    }
    // Use @lid JIDs as-is — they are valid routable identifiers in Baileys
    const jid = to.includes('@') ? to : `${to}@s.whatsapp.net`;
    await sock.sendMessage(jid, { text });
    res.json({ success: true });
});

app.post('/send-buttons', async (req, res) => {
    const { to, text, buttons } = req.body || {};
    if (!to || !text || !buttons) return res.status(400).json({ error: 'to, text, buttons required' });
    if (!sock || connectionStatus !== 'connected') {
        return res.status(503).json({ error: 'Not connected to WhatsApp' });
    }
    try {
        const jid = to.includes('@') ? to : `${to}@s.whatsapp.net`;
        const waButtons = buttons.slice(0, 3).map((b) => ({
            buttonId: b.id,
            buttonText: { displayText: b.title.slice(0, 20) },
            type: 1,
        }));
        await sock.sendMessage(jid, {
            text,
            buttons: waButtons,
            headerType: 1,
        });
        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

app.post('/typing', async (req, res) => {
    const { to, state } = req.body || {};
    if (!to) return res.status(400).json({ error: 'to required' });
    if (!sock || connectionStatus !== 'connected') {
        return res.status(503).json({ error: 'Not connected to WhatsApp' });
    }
    const jid = to.includes('@') ? to : `${to}@s.whatsapp.net`;
    try {
        await sock.sendPresenceUpdate(state === 'paused' ? 'paused' : 'composing', jid);
        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

app.post('/send-file', async (req, res) => {
    const { to, filePath, caption, mimeType } = req.body || {};
    if (!to || !filePath) {
        return res.status(400).json({ error: 'to and filePath required' });
    }
    if (!sock || connectionStatus !== 'connected') {
        return res.status(503).json({ error: 'Not connected to WhatsApp' });
    }
    const jid = to.includes('@') ? to : `${to}@s.whatsapp.net`;
    try {
        const fileBuffer = fs.readFileSync(filePath);
        await sock.sendMessage(jid, {
            document: fileBuffer,
            mimetype: mimeType || 'application/octet-stream',
            fileName: path.basename(filePath),
            caption: caption || undefined,
        });
        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

app.post('/logout', async (req, res) => {
    try {
        if (sock) await sock.logout();
        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// ---- Start ----

app.listen(PORT, '127.0.0.1', () => {
    console.log(`[whatsapp-bridge] Listening on 127.0.0.1:${PORT}`);
    startBaileys().catch((e) => console.error('[whatsapp-bridge] Baileys start error:', e));
});

process.on('SIGTERM', async () => {
    isShuttingDown = true;
    console.log('[whatsapp-bridge] Shutting down');
    try {
        if (sock) await sock.end();
    } catch (_) {}
    process.exit(0);
});
