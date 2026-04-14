import MessageFeed from '../../components/MessageFeed';

interface Props {
  params: { channelId: string };
}

export default function ChannelPage({ params }: Props) {
  return <MessageFeed channelId={params.channelId} />;
}
