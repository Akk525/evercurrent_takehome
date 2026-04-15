import DMView from '../../../components/DMView';

export default function DMPage({ params }: { params: { userId: string } }) {
  return <DMView userId={params.userId} />;
}
