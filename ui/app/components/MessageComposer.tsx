'use client';

interface MessageComposerProps {
  placeholder: string;
  disabled?: boolean;
}

export default function MessageComposer({ placeholder, disabled = true }: MessageComposerProps) {
  return (
    <div className="px-4 pb-4 pt-2 flex-shrink-0">
      <div className="flex items-center gap-2 border border-gray-300 rounded-lg px-3 py-2 bg-white hover:border-gray-400 transition-colors">
        {/* Plus button */}
        <button
          disabled={disabled}
          className="text-gray-400 hover:text-gray-600 text-lg leading-none flex-shrink-0 disabled:cursor-default"
          title="Attach"
        >
          +
        </button>

        {/* Input area */}
        <div className="flex-1 text-sm text-gray-400 cursor-default select-none">
          {placeholder}
        </div>

        {/* Right icons */}
        <div className="flex items-center gap-2 text-gray-400 flex-shrink-0">
          <button disabled={disabled} className="hover:text-gray-600 disabled:cursor-default text-base" title="Emoji">
            😊
          </button>
          <button disabled={disabled} className="hover:text-gray-600 disabled:cursor-default" title="Send">
            <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor">
              <path d="M10.894 2.553a1 1 0 00-1.788 0l-7 14a1 1 0 001.169 1.409l5-1.429A1 1 0 009 15.571V11a1 1 0 112 0v4.571a1 1 0 00.725.962l5 1.428a1 1 0 001.17-1.408l-7-14z" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}
