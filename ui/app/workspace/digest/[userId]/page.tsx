import DigestView from '../../../components/DigestView';

interface Props {
  params: { userId: string };
}

export default function DigestPage({ params }: Props) {
  return <DigestView userId={params.userId} />;
}
