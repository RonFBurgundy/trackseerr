export const PASSWORD_MIN_LENGTH = 12;
export const PASSWORD_MAX_LENGTH = 128;

export interface PasswordChecks {
  lengthOk: boolean;
  notContainsUsername: boolean;
  matches: boolean;
  /** True when every client-side hint passes. The server remains the authority. */
  allOk: boolean;
}

export function checkPassword(password: string, confirm: string, username: string): PasswordChecks {
  const lengthOk = password.length >= PASSWORD_MIN_LENGTH && password.length <= PASSWORD_MAX_LENGTH;
  const name = username.trim().toLowerCase();
  const notContainsUsername = name.length === 0 || !password.toLowerCase().includes(name);
  const matches = password.length > 0 && password === confirm;
  return { lengthOk, notContainsUsername, matches, allOk: lengthOk && notContainsUsername && matches };
}

export interface PasswordStrength {
  /** 0 (empty) to 4 (strong). */
  score: 0 | 1 | 2 | 3 | 4;
  label: string;
}

/** A coarse hint only: length plus character variety, with a penalty for repeated characters. */
export function passwordStrength(password: string): PasswordStrength {
  if (!password) return { score: 0, label: 'Empty' };
  let points = 0;
  if (password.length >= PASSWORD_MIN_LENGTH) points += 1;
  if (password.length >= 16) points += 1;
  if (password.length >= 20) points += 1;
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((re) => re.test(password)).length;
  if (classes >= 3) points += 1;
  if (new Set(password).size < Math.ceil(password.length / 3)) points -= 2;
  const score = Math.max(0, Math.min(4, points)) as 0 | 1 | 2 | 3 | 4;
  const labels = ['Very weak', 'Weak', 'Fair', 'Good', 'Strong'] as const;
  return { score, label: labels[score] };
}
