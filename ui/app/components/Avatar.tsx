interface AvatarProps {
  name: string;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
}

const COLORS = [
  'bg-purple-500', 'bg-blue-500', 'bg-green-500', 'bg-yellow-500',
  'bg-red-500', 'bg-indigo-500', 'bg-pink-500', 'bg-teal-500',
];

function getColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return COLORS[Math.abs(hash) % COLORS.length];
}

function getInitials(name: string): string {
  return name.split(' ').map(n => n[0]).join('').slice(0, 2).toUpperCase();
}

export default function Avatar({ name, size = 'md', className = '' }: AvatarProps) {
  const sizeClass = size === 'sm' ? 'w-7 h-7 text-xs' : size === 'lg' ? 'w-10 h-10 text-sm' : 'w-8 h-8 text-sm';
  return (
    <div className={`${sizeClass} ${getColor(name)} rounded flex items-center justify-center text-white font-semibold flex-shrink-0 ${className}`}>
      {getInitials(name)}
    </div>
  );
}
