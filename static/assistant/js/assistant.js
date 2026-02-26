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

        sendMessage() {
            const input = this.$refs.messageInput;
            const message = input.value.trim();
            if (!message || this.loading) return;

            // Trigger HTMX submit
            this.$refs.chatForm.requestSubmit();
        },

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

            // Clear input
            input.value = '';
            input.style.height = 'auto';
        },

        afterSend(event) {
            this.loading = false;

            // Remove typing indicator
            const typing = document.getElementById('typing-indicator');
            if (typing) typing.remove();

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
