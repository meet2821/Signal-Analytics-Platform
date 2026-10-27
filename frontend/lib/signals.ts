export const SIGNALS = [
  "motor_current",
  "motor_speed",
  "torque",
  "vibration",
  "rolling_force",
  "temperature",
] as const;

export const SIGNAL_META: Record<string, { label: string; unit: string; color: string }> = {
  motor_current: { label: "Motor current", unit: "A", color: "#38bdf8" },
  motor_speed: { label: "Motor speed", unit: "rpm", color: "#a78bfa" },
  torque: { label: "Torque", unit: "kNm", color: "#f472b6" },
  vibration: { label: "Vibration", unit: "mm/s", color: "#fb923c" },
  rolling_force: { label: "Rolling force", unit: "kN", color: "#34d399" },
  temperature: { label: "Temperature", unit: "°C", color: "#facc15" },
};
