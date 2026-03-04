/**
 * ERPlora AI Assistant — Alpine.js Chat Component + Global Chat Functions
 *
 * Global functions (submitChat, startNewConversation, etc.) are defined here
 * so they are available regardless of whether the page loaded via full reload
 * or HTMX partial swap (inline scripts in partials don't run in global scope).
 */

var _chatFileData = null;
var _chatUrl = '/m/assistant/chat/send/';

function startNewConversation() {
    document.getElementById('conv-id').value = '';
    var msgs = document.getElementById('chat-messages');
    msgs.innerHTML =
        '<div class="chat-message chat-assistant">' +
            '<div class="chat-bubble">' +
                '<p>Hello! I\'m your AI assistant. I can help you configure your hub, manage modules, create products, employees, and more.</p>' +
                '<p class="mt-2 text-sm opacity-75">Try asking me something like:</p>' +
                '<ul class="mt-1 text-sm opacity-75 list-disc pl-4">' +
                    '<li>What modules are available?</li>' +
                    '<li>Show me the current configuration</li>' +
                    '<li>Create an employee named Juan with manager role</li>' +
                '</ul>' +
            '</div>' +
        '</div>';
    clearFileAttachment();
    var input = document.getElementById('chat-input');
    input.value = '';
    input.disabled = false;
    input.focus();
    document.getElementById('chat-submit').disabled = false;
    document.getElementById('chat-submit').classList.remove('is-loading');
}

document.addEventListener('htmx:afterSwap', function(event) {
    var msgs = document.getElementById('chat-messages');
    if (msgs && msgs.contains(event.detail.target)) {
        msgs.scrollTop = msgs.scrollHeight;
    }
});

function handleFileSelect(input) {
    var file = input.files[0];
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) {
        if (window.showToast) showToast('File too large. Maximum size is 10 MB.', 'warning');
        input.value = '';
        return;
    }
    var allowed = ['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'application/pdf'];
    if (allowed.indexOf(file.type) === -1) {
        if (window.showToast) showToast('Unsupported file type. Please use JPEG, PNG, WebP, GIF, or PDF.', 'warning');
        input.value = '';
        return;
    }
    _chatFileData = file;
    var preview = document.getElementById('file-preview');
    var previewImg = document.getElementById('file-preview-img');
    var previewIcon = document.getElementById('file-preview-icon');
    document.getElementById('file-preview-name').textContent = file.name;
    preview.classList.remove('hidden');
    if (file.type.startsWith('image/')) {
        var reader = new FileReader();
        reader.onload = function(e) {
            previewImg.src = e.target.result;
            previewImg.classList.remove('hidden');
            previewImg.classList.add('active');
        };
        reader.readAsDataURL(file);
        previewIcon.classList.add('hidden');
        previewIcon.classList.remove('active');
    } else {
        previewImg.classList.add('hidden');
        previewImg.classList.remove('active');
        previewIcon.classList.remove('hidden');
        previewIcon.classList.add('active');
    }
}

function clearFileAttachment() {
    _chatFileData = null;
    var fileInput = document.getElementById('chat-file');
    if (fileInput) fileInput.value = '';
    var preview = document.getElementById('file-preview');
    if (preview) preview.classList.add('hidden');
    var img = document.getElementById('file-preview-img');
    if (img) { img.classList.add('hidden'); img.classList.remove('active'); }
    var icon = document.getElementById('file-preview-icon');
    if (icon) { icon.classList.add('hidden'); icon.classList.remove('active'); }
}

function submitChat() {
    var input = document.getElementById('chat-input');
    var msg = input.value.trim();
    if (!msg && !_chatFileData) return;

    var msgs = document.getElementById('chat-messages');
    var submitBtn = document.getElementById('chat-submit');

    var bubbleHtml = '';
    if (_chatFileData && _chatFileData.type.startsWith('image/')) {
        bubbleHtml += '<img src="' + document.getElementById('file-preview-img').src + '" class="max-h-32 rounded mb-2" alt="Attached">';
    } else if (_chatFileData) {
        bubbleHtml += '<div class="text-sm opacity-75 mb-1">\uD83D\uDCC4 ' + _chatFileData.name + '</div>';
    }
    if (msg) bubbleHtml += msg.replace(/</g, '&lt;').replace(/>/g, '&gt;');

    var bubble = document.createElement('div');
    bubble.className = 'chat-message chat-user';
    bubble.innerHTML = '<div class="chat-bubble">' + bubbleHtml + '</div>';
    msgs.appendChild(bubble);

    var typing = document.createElement('div');
    typing.id = 'typing-indicator';
    typing.className = 'chat-message chat-assistant';
    typing.innerHTML = '<div class="chat-typing"><span class="chat-typing-dot"></span><span class="chat-typing-dot"></span><span class="chat-typing-dot"></span></div>';
    msgs.appendChild(typing);
    msgs.scrollTop = msgs.scrollHeight;

    input.disabled = true;
    submitBtn.disabled = true;
    submitBtn.classList.add('is-loading');

    var formData = new FormData();
    formData.append('message', msg);
    formData.append('conversation_id', document.getElementById('conv-id').value);
    formData.append('context', document.querySelector('[name=context]') ? document.querySelector('[name=context]').value : 'general');
    formData.append('csrfmiddlewaretoken', document.querySelector('[name=csrfmiddlewaretoken]').value);
    if (_chatFileData) formData.append('file', _chatFileData);

    input.value = '';
    input.style.height = 'auto';
    clearFileAttachment();

    fetch(_chatUrl, {
        method: 'POST',
        body: formData,
        headers: {'HX-Request': 'true'},
    })
    .then(function(response) {
        var convId = response.headers.get('X-Conversation-Id');
        if (convId) document.getElementById('conv-id').value = convId;
        return response.text();
    })
    .then(function(html) {
        var t = document.getElementById('typing-indicator');
        if (t) t.remove();
        msgs.insertAdjacentHTML('beforeend', html);
        msgs.scrollTop = msgs.scrollHeight;
        if (window.htmx) htmx.process(msgs.lastElementChild);
        input.disabled = false;
        submitBtn.disabled = false;
        submitBtn.classList.remove('is-loading');
        input.focus();
    })
    .catch(function() {
        var t = document.getElementById('typing-indicator');
        if (t) t.remove();
        msgs.insertAdjacentHTML('beforeend',
            '<div class="chat-message chat-assistant"><div class="chat-bubble">Error: Could not reach AI service.</div></div>');
        msgs.scrollTop = msgs.scrollHeight;
        input.disabled = false;
        submitBtn.disabled = false;
        submitBtn.classList.remove('is-loading');
        input.focus();
    });
}

function toggleVoiceInput(btn) {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return;
    if (btn._recognition) {
        btn._recognition.stop();
        btn._recognition = null;
        btn.classList.remove('recording', 'color-error');
        return;
    }
    var recognition = new SR();
    recognition.lang = document.documentElement.lang || 'en';
    recognition.interimResults = true;
    recognition.continuous = false;
    var input = document.getElementById('chat-input');
    var beforeText = input.value;
    recognition.onresult = function(event) {
        var transcript = '';
        for (var i = 0; i < event.results.length; i++) transcript += event.results[i][0].transcript;
        input.value = beforeText + (beforeText ? ' ' : '') + transcript;
    };
    recognition.onend = function() { btn._recognition = null; btn.classList.remove('recording', 'color-error'); };
    recognition.onerror = function() { btn._recognition = null; btn.classList.remove('recording', 'color-error'); };
    btn._recognition = recognition;
    btn.classList.add('recording', 'color-error');
    recognition.start();
}

/**
 * ERPlora AI Assistant — Alpine.js Chat Component
 */
function assistantChat() {
    return {
        loading: false,
        conversationId: '',
        tierName: '',
        sessionsUsed: 0,
        sessionsLimit: 0,

        // Voice
        recording: false,
        speechSupported: !!(window.SpeechRecognition || window.webkitSpeechRecognition),
        _recognition: null,

        beforeSend(event) {
            const input = this.$refs.messageInput;
            const message = input.value.trim();
            if (!message) {
                event.preventDefault();
                return;
            }

            this.loading = true;

            // Append user message bubble immediately
            const messagesEl = this.$refs.messages;
            const userBubble = document.createElement('div');
            userBubble.className = 'chat-message chat-user';
            userBubble.innerHTML = '<div class="chat-bubble">' + this.escapeHtml(message) + '</div>';
            messagesEl.appendChild(userBubble);

            // Add typing indicator
            const typing = document.createElement('div');
            typing.className = 'chat-message chat-assistant';
            typing.id = 'typing-indicator';
            typing.innerHTML = '<div class="chat-typing">'
                + '<span class="chat-typing-dot"></span>'
                + '<span class="chat-typing-dot"></span>'
                + '<span class="chat-typing-dot"></span>'
                + '</div>';
            messagesEl.appendChild(typing);

            this.scrollToBottom();
        },

        afterSend(event) {
            this.loading = false;

            // Remove typing indicator
            const typing = document.getElementById('typing-indicator');
            if (typing) typing.remove();

            // Clear input and reset height
            const input = this.$refs.messageInput;
            input.value = '';
            input.style.height = 'auto';

            // Extract headers from response
            const xhr = event.detail.xhr;
            if (xhr) {
                const convId = xhr.getResponseHeader('X-Conversation-Id');
                if (convId) {
                    this.conversationId = convId;
                }

                // Update tier/usage info
                const tierHeader = xhr.getResponseHeader('X-Assistant-Tier');
                const usageHeader = xhr.getResponseHeader('X-Assistant-Usage');
                if (usageHeader) {
                    try {
                        const usage = JSON.parse(usageHeader);
                        this.tierName = usage.tier_name || tierHeader || '';
                        this.sessionsUsed = usage.sessions_used || 0;
                        this.sessionsLimit = usage.sessions_limit || 0;
                    } catch (e) {
                        // ignore parse errors
                    }
                }
            }

            this.scrollToBottom();
        },

        // --- Voice-to-text ---

        toggleVoice() {
            if (this.recording) {
                this.stopVoice();
            } else {
                this.startVoice();
            }
        },

        startVoice() {
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            if (!SpeechRecognition) return;

            const recognition = new SpeechRecognition();
            recognition.lang = document.documentElement.lang || 'en';
            recognition.interimResults = true;
            recognition.continuous = false;

            const input = this.$refs.messageInput;
            const beforeText = input.value;

            recognition.onresult = (event) => {
                let transcript = '';
                for (let i = 0; i < event.results.length; i++) {
                    transcript += event.results[i][0].transcript;
                }
                // Append transcript to existing text
                input.value = beforeText + (beforeText ? ' ' : '') + transcript;
                this.autoResize(input);
            };

            recognition.onend = () => {
                this.recording = false;
                this._recognition = null;
            };

            recognition.onerror = (event) => {
                this.recording = false;
                this._recognition = null;
                // 'no-speech' and 'aborted' are not real errors
                if (event.error !== 'no-speech' && event.error !== 'aborted') {
                    console.warn('[Assistant] Speech recognition error:', event.error);
                }
            };

            this._recognition = recognition;
            this.recording = true;
            recognition.start();
        },

        stopVoice() {
            if (this._recognition) {
                this._recognition.stop();
            }
            this.recording = false;
        },

        // --- Utilities ---

        scrollToBottom() {
            this.$nextTick(() => {
                const el = this.$refs.messages;
                if (el) {
                    el.scrollTop = el.scrollHeight;
                }
            });
        },

        autoResize(el) {
            el.style.height = 'auto';
            el.style.height = Math.min(el.scrollHeight, 120) + 'px';
        },

        escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
    };
}
