// Slider-style toggle: green track + thumb-right when on, red when off.
// Drop-in replacement for boolean checkboxes and on/off buttons.
type Props = {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  title?: string;
  // Optional label rendered to the right of the switch. If `showState` is
  // true (default), the label text is "ON" / "OFF" in green/red.
  label?: string;
  showState?: boolean;
};

export function Toggle({ checked, onChange, disabled, title, label, showState = true }: Props) {
  return (
    <label
      className="toggle"
      title={title}
      onClick={(e) => {
        // Prevent parent row click handlers from firing when user flips the switch
        e.stopPropagation();
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="toggle-track" aria-hidden="true" />
      {label ? (
        <span className="toggle-label">{label}</span>
      ) : showState ? (
        <span className={`toggle-label ${checked ? 'on' : 'off'}`}>{checked ? 'ON' : 'OFF'}</span>
      ) : null}
    </label>
  );
}
