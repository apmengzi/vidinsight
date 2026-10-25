import { FC, useState } from 'react'
import { useInView } from 'react-intersection-observer'
import clsx from 'clsx'

interface LazyImageProps {
  src: string
  alt?: string
  className?: string
  placeholder?: string
}

const LazyImage: FC<LazyImageProps> = ({ src, alt, className, placeholder }) => {
  const { ref, inView } = useInView({
    triggerOnce: true,
    rootMargin: '160px 0px',
    threshold: 0.01,
  })
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState(false)
  const showImage = Boolean(inView && src && !error)

  return (
    <div
      ref={ref}
      className={clsx(
        'relative overflow-hidden bg-[var(--vi-hairline)]',
        className || 'h-10 w-14 rounded-md'
      )}
    >
      {/* 图片未加载完成或未进入视口时的同色脉冲微光占位 */}
      {!loaded && !error && (
        <div className="absolute inset-0 animate-pulse bg-[var(--vi-hairline)]" aria-label={alt || 'loading'} />
      )}

      {showImage ? (
        <img
          src={src}
          alt={alt}
          loading="lazy"
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => setError(true)}
          className={clsx(
            'h-full w-full object-cover transition-opacity duration-300 ease-out',
            loaded ? 'opacity-100' : 'opacity-0'
          )}
        />
      ) : placeholder ? (
        <img src={placeholder} alt={alt || 'cover'} className="h-full w-full object-cover opacity-60" />
      ) : (
        <div className="h-full w-full animate-pulse bg-[var(--vi-hairline)]" aria-label={alt || 'cover'} />
      )}
    </div>
  )
}

export default LazyImage
