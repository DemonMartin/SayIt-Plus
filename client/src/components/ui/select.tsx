import { forwardRef, useState, useRef, useEffect } from 'react'
import { ChevronDown, Check } from 'lucide-react'
import { useT } from '@/i18n/useT'
import { cn } from '@/lib/utils'

export interface SelectOption {
  value: string
  label: string
  /**
   * 悬停提示。只有在 label 可能被 truncate 截掉、而完整内容对用户有用时才设
   * （麦克风列表就是：设备名很长，但那串后缀正是辨认设备的依据）。
   * 不设就没有 tooltip —— 别给所有下拉都挂上，那是噪音。
   */
  title?: string
  /** 列表里跟在 label 后面的灰色次要信息（如供应商名），与 label 同一行。触发器上不显示。 */
  hint?: string
}

export interface SelectProps {
  value: string
  onChange: (value: string) => void
  options?: SelectOption[]
  children?: React.ReactNode
  className?: string
  placeholder?: string
  disabled?: boolean
  /** 关联可见标题的 id。触发器是个 <button>，屏幕阅读器只会念它内部的当前值，
   *  旁边那行 <label> 文本不会自动关联（没有 htmlFor 可指）。与 Segmented 同款。 */
  labelledBy?: string
  /** sm：设置行右侧那种紧凑下拉（h-8、12px 字、选项行更矮）。 */
  size?: 'default' | 'sm'
  /**
   * 弹出列表的宽度。stretch（默认）= 与触发器同宽；end = 右对齐、按内容撑开，
   * 最宽 20rem。触发器放在一行右侧又比较窄时用 end：列表往左长，不会伸出卡片，
   * 长名字也不用截得只剩半截。
   */
  menuAlign?: 'stretch' | 'end'
}

const Select = forwardRef<HTMLDivElement, SelectProps>(
  ({ value, onChange, options, children, className, placeholder, disabled = false, labelledBy, size = 'default', menuAlign = 'stretch' }, ref) => {
    const sm = size === 'sm'
    const t = useT()
    const [isOpen, setIsOpen] = useState(false)
    const containerRef = useRef<HTMLDivElement>(null)

    // 如果传入了 children（原生 option），则解析它们
    const parsedOptions: SelectOption[] = options || []

    if (!options && children) {
      const childArray = Array.isArray(children) ? children : [children]
      childArray.forEach((child: any) => {
        if (child?.type === 'option') {
          parsedOptions.push({
            value: child.props.value || '',
            label: child.props.children || '',
          })
        }
      })
    }

    const selectedOption = parsedOptions.find((opt) => opt.value === value)

    useEffect(() => {
      const handleClickOutside = (event: MouseEvent) => {
        if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
          setIsOpen(false)
        }
      }

      if (isOpen) {
        document.addEventListener('mousedown', handleClickOutside)
        return () => document.removeEventListener('mousedown', handleClickOutside)
      }
    }, [isOpen])

    const handleSelect = (optionValue: string) => {
      onChange(optionValue)
      setIsOpen(false)
    }

    return (
      <div ref={containerRef} className={cn('relative', className)}>
        <button
          ref={ref as any}
          type="button"
          onClick={() => !disabled && setIsOpen(!isOpen)}
          disabled={disabled}
          aria-labelledby={labelledBy}
          aria-haspopup="listbox"
          aria-expanded={isOpen}
          className={cn(
            'flex w-full items-center justify-between rounded-md border border-input-border bg-input-bg text-foreground transition-colors',
            sm ? 'h-8 px-2.5 text-xs' : 'h-9 px-3 text-sm',
            'hover:border-muted-foreground/40 focus:border-input-focus-border focus:outline-none focus:ring-2 focus:ring-input-focus-ring/20',
            'disabled:cursor-not-allowed disabled:opacity-50',
            isOpen && 'border-input-focus-border ring-2 ring-input-focus-ring/20',
          )}
        >
          <span
            className={cn('min-w-0 truncate', !selectedOption && 'text-input-placeholder')}
            title={selectedOption?.title}
          >
            {/* hint 只在列表里显示：触发器通常很窄，带上它只会把次要信息截成半截 */}
            {selectedOption?.label || placeholder || t('ui.selectPlaceholder')}
          </span>
          <ChevronDown
            className={cn(
              'shrink-0 text-muted-foreground transition-transform',
              sm ? 'ml-1.5 h-3.5 w-3.5' : 'ml-2 h-4 w-4',
              isOpen && 'rotate-180',
            )}
          />
        </button>

        {isOpen && (
          <div
            className={cn(
              'absolute z-50 mt-1 rounded-md border border-border bg-card shadow-lg',
              menuAlign === 'end' ? 'right-0 w-max min-w-full max-w-xs' : 'w-full',
            )}
          >
            <div className={cn('custom-scrollbar max-h-60 overflow-y-auto', sm ? 'p-1' : 'py-1')}>
              {parsedOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => handleSelect(option.value)}
                  className={cn(
                    'flex w-full items-center justify-between text-left transition-colors',
                    sm ? 'rounded px-2 py-1.5 text-xs' : 'px-3 py-2 text-sm',
                    option.value === value
                      ? 'bg-primary/5 text-primary font-medium'
                      : 'text-foreground hover:bg-accent',
                  )}
                >
                  <span className="min-w-0 truncate" title={option.title}>
                    {option.label}
                    {option.hint && (
                      <span className={cn('ml-1.5 font-normal text-muted-foreground', sm && 'text-[11px]')}>{option.hint}</span>
                    )}
                  </span>
                  {option.value === value && <Check className={cn('shrink-0', sm ? 'ml-2 h-3.5 w-3.5' : 'ml-2 h-4 w-4')} />}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    )
  },
)

Select.displayName = 'Select'

export { Select }
